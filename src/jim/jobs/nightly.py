"""Nightly job (~21:00 local): housekeeping only — sync today's data into
Postgres, reconcile today's adherence, and sweep stale one-off Garmin
adaptations. It does NOT plan tomorrow; the athlete gets plan edits by talking
to the coach (see coach.py), not from an unsolicited draft written overnight.

This still has to run nightly because coach.py's fetch_state() reads
query_history/readiness_read, which read the tables sync_today fills — without
this job, readiness/volume features and adherence tracking go stale.

Two entrypoints, same work:
- Vercel Cron -> GET /api/cron/nightly (see app.py), the deployed path.
- `python -m jim.jobs.nightly`, for running it by hand.
"""

import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jim.config import settings
from jim.db import connect, ensure_migrated

log = logging.getLogger(__name__)


STRENGTH_TYPES = ("strength_training", "fitness_equipment")


def store_exercise_sets(conn, user_id: int, activity_id: str, day, sets: list[dict]) -> None:
    """Upsert an activity's ACTIVE sets (per-exercise reps/weights)."""
    for s in sets:
        conn.execute(
            "INSERT INTO exercise_sets (user_id, activity_id, set_index, day, category,"
            " exercise_name, reps, weight_kg, duration_sec)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (user_id, activity_id, set_index) DO NOTHING",
            (user_id, activity_id, s["set_index"], day, s.get("category"),
             s.get("exercise_name"), s.get("reps"), s.get("weight_kg"),
             s.get("duration_sec")),
        )


def _today_for_user(user_id: int) -> date:
    """Resolve 'today' from the user's own timezone (users.timezone), falling
    back to the global app_timezone default only if it's somehow unset."""
    tz = settings().app_timezone
    with connect() as conn:
        row = conn.execute(
            "SELECT timezone FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    if row and row.get("timezone"):
        tz = row["timezone"]
    return datetime.now(ZoneInfo(tz)).date()


def sync_today(user_id: int) -> None:
    """Persist today's Garmin state so query_history has fresh rows."""
    from jim.tools.garmin import get_exercise_sets, get_garmin_today

    today = _today_for_user(user_id)
    garmin = get_garmin_today(user_id, today)

    with connect() as conn:
        conn.execute(
            "INSERT INTO garmin_daily (user_id, day, hrv, sleep_hours, body_battery,"
            " readiness, resting_hr, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (user_id, day) DO UPDATE SET hrv=EXCLUDED.hrv,"
            " sleep_hours=EXCLUDED.sleep_hours, body_battery=EXCLUDED.body_battery,"
            " readiness=EXCLUDED.readiness, resting_hr=EXCLUDED.resting_hr, raw=EXCLUDED.raw",
            (user_id, today, garmin.hrv, garmin.sleep_hours, garmin.body_battery,
             garmin.readiness, garmin.resting_hr, garmin.model_dump_json()),
        )
        for act in garmin.activities:
            conn.execute(
                "INSERT INTO garmin_activities (user_id, activity_id, day, type,"
                " duration_min, training_load, summary) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (user_id, activity_id) DO NOTHING",
                (user_id, act.activity_id, today, act.type, act.duration_min,
                 act.training_load, act.model_dump_json()),
            )
            if act.type in STRENGTH_TYPES:
                try:
                    store_exercise_sets(
                        conn, user_id, act.activity_id, today,
                        get_exercise_sets(user_id, act.activity_id),
                    )
                except Exception:
                    log.exception("exercise sets fetch failed for %s", act.activity_id)
        conn.commit()


def cleanup_stale_adaptations(user_id: int, today: date) -> None:
    """Delete one-off Garmin workouts (built for a single adapted day, never
    promoted into the playbook) whose day has already passed — see the
    "jim_created_workouts" kv entry written by coach._push_one. A failed
    delete just leaves the entry for tomorrow's sweep to retry.

    This is coach.py's own bookkeeping — it only knows about workouts *it*
    created. See cleanup_adapted_workouts below for the MCP path's
    equivalent, which has no such kv entry to read."""
    from jim.db import kv_get, kv_set
    from jim.tools import garmin

    created = kv_get(user_id, "jim_created_workouts") or {}
    for fd in [d for d in created if date.fromisoformat(d) < today]:
        try:
            garmin.delete_garmin_workout(user_id, created[fd]["workout_id"])
        except Exception:
            log.warning("couldn't delete stale workout for user %s on %s",
                        user_id, fd, exc_info=True)
            continue
        del created[fd]
    kv_set(user_id, "jim_created_workouts", created)


def cleanup_adapted_workouts(user_id: int, today: date, lookback_days: int = 30) -> None:
    """The MCP path's equivalent of cleanup_stale_adaptations: Claude creates
    one-off workouts directly via mcp_server.create_or_update_workout, with
    no Jim-side record of having done so (no coach.py, no kv entry) — Garmin
    itself is the only source of truth. So instead of reading a kv map, this
    scans the athlete's actual Garmin calendar for the trailing window,
    finds items titled with ADAPTED_WORKOUT_PREFIX whose date has already
    passed, and deletes them. Only scheduled-and-past items are covered; an
    adapted workout created but never scheduled has no date to sweep by and
    is a known gap (rare in practice — creation and scheduling happen in the
    same conversational turn)."""
    from jim.tools import garmin

    start = today - timedelta(days=lookback_days)
    end = today - timedelta(days=1)
    if start > end:
        return
    scheduled = garmin.get_scheduled_workouts(user_id, start, end)
    for item in scheduled:
        if not item["title"].startswith(garmin.ADAPTED_WORKOUT_PREFIX):
            continue
        try:
            garmin.delete_garmin_workout(user_id, item["workout_id"])
        except Exception:
            log.warning("couldn't delete adapted workout %s for user %s on %s",
                        item["workout_id"], user_id, item["date"], exc_info=True)


def _run_nightly_for_user(user_id: int) -> dict:
    """Sync today's data, sweep stale adaptations, and close today's loop, for
    `user_id`. Housekeeping only — no plan is written here.

    Returns a summary (incl. elapsed seconds) so the caller can see how close the
    run is to a serverless timeout — this is invoked from Vercel Cron, where the
    whole thing must finish inside the function's maxDuration.
    """
    from jim.jobs.reconcile import reconcile_day

    started = time.monotonic()
    ensure_migrated()
    sync_today(user_id)
    today = _today_for_user(user_id)
    try:
        cleanup_stale_adaptations(user_id, today)
    except Exception:
        # Cleanup is housekeeping, not the sync itself — a Garmin hiccup here
        # must not stop today's reconcile from running.
        log.warning("adaptation cleanup failed for user %s", user_id, exc_info=True)
    try:
        cleanup_adapted_workouts(user_id, today)
    except Exception:
        log.warning("MCP adaptation cleanup failed for user %s", user_id, exc_info=True)
    reconcile_day(user_id, today)
    elapsed = round(time.monotonic() - started, 1)
    log.info("nightly housekeeping done in %ss for user %s", elapsed, user_id)
    return {"elapsed_sec": elapsed}


def run_nightly() -> dict:
    """Fan out the nightly run over every nightly_enabled user.

    One user's failure (expired Garmin creds, a Garmin API hiccup during
    cleanup/reconcile) is caught and logged right here, at the per-user
    boundary — it must not stop the rest of the cron run.
    """
    started = time.monotonic()
    ensure_migrated()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE nightly_enabled = true"
        ).fetchall()
    results: dict[int, dict] = {}
    for row in rows:
        uid = row["id"]
        try:
            results[uid] = _run_nightly_for_user(uid)
        except Exception:
            log.exception("nightly failed for user %s", uid)
            results[uid] = {"error": True}
    return {"users": results, "elapsed_sec": round(time.monotonic() - started, 1)}


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    ensure_migrated()
    result = run_nightly()
    for uid, summary in result["users"].items():
        log.info("user %s: %s", uid, summary)
    log.info(
        "nightly fan-out done in %ss over %d user(s)",
        result["elapsed_sec"], len(result["users"]),
    )


if __name__ == "__main__":
    main()
