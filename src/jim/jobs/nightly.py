"""Nightly job (~21:00 local): housekeeping only — sync today's data into
Postgres, reconcile today's adherence, and sweep stale one-off Garmin
adaptations. It does NOT plan tomorrow; the athlete gets plan edits by
talking to Claude through the Garmin MCP server (see mcp_server.py), not
from an unsolicited draft written overnight.

A user with zero rows in garmin_daily (brand new signup) gets a one-time
~90-day backfill — see tools/garmin.backfill_if_empty, triggered from
web/garmin_routes.py right when they connect Garmin, and again from
mcp_server.py's read tools as a second safety net. Not from here: one new
signup's ~90 sequential Garmin calls could blow the whole cron run's 60s
Vercel budget for every other user sharing that invocation, so it
deliberately lives outside this module.

This still has to run nightly because the readiness/history read tools
(tools/history.py) read the tables sync_today fills — without this job,
readiness/volume features and adherence tracking go stale.

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


def cleanup_adapted_workouts(user_id: int, today: date, lookback_days: int = 30) -> None:
    """Claude creates one-off workouts directly via
    mcp_server.create_or_update_workout, with no Jim-side record of having
    done so — Garmin itself is the only source of truth.

    This used to scan get_scheduled_workouts (the calendar view) for
    past-dated items, but Garmin drops a workout from that view once it's
    been completed — moved to activity history, not upcoming plans. A
    one-off adapted workout done on time and then swept by the *next*
    night's cron was fine, but if Garmin removed it from the calendar
    before that cron ran, cleanup had no date left to check against and it
    became a permanent orphan (an actual case: a completed walk survived
    for days with no scheduled-date trace anywhere). Scanning the real
    workout library instead — which doesn't lose entries on completion —
    and keying off each workout's own createdDate closes that race. Also
    incidentally covers the old known gap of an adapted workout created but
    never scheduled, which had no calendar date to sweep by at all."""
    from jim.tools import garmin

    start = today - timedelta(days=lookback_days)
    end = today - timedelta(days=1)
    if start > end:
        return
    for w in garmin.list_garmin_workouts(user_id):
        if not w["name"].startswith(garmin.ADAPTED_WORKOUT_PREFIX):
            continue
        try:
            detail = garmin.get_garmin_workout_detail(user_id, w["workout_id"])
            created_raw = detail.get("createdDate") or detail.get("updatedDate")
            created = date.fromisoformat(created_raw[:10]) if created_raw else None
        except Exception:
            log.warning("couldn't read created date for workout %s (user %s)",
                        w["workout_id"], user_id, exc_info=True)
            continue
        if created is None or not (start <= created <= end):
            continue
        try:
            garmin.delete_garmin_workout(user_id, w["workout_id"])
        except Exception:
            log.warning("couldn't delete adapted workout %s for user %s (created %s)",
                        w["workout_id"], user_id, created, exc_info=True)


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
    today = _today_for_user(user_id)
    sync_today(user_id)
    try:
        cleanup_adapted_workouts(user_id, today)
    except Exception:
        # Cleanup is housekeeping, not the sync itself — a Garmin hiccup here
        # must not stop today's reconcile from running.
        log.warning("adaptation cleanup failed for user %s", user_id, exc_info=True)
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
