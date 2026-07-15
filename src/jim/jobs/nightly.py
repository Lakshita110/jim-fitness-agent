"""Nightly job (~21:00 local): sync today's data into Postgres, reconcile, then
plan tomorrow.

Two entrypoints, same work:
- Vercel Cron -> GET /api/cron/nightly (see app.py), the deployed path.
- `python -m jim.jobs.nightly`, for running it by hand.
"""

import json
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from jim.agent.loop import run_agent
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


def store_notion_log(conn, user_id: int, notion) -> None:
    """Upsert one day of the knee/habit log. Shared with scripts/backfill.py."""
    conn.execute(
        "INSERT INTO notion_daily_log (user_id, day, pain_level, pain_location, pain_notes,"
        " pt_done, habits, day_score) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (user_id, day) DO UPDATE SET pain_level=EXCLUDED.pain_level,"
        " pain_location=EXCLUDED.pain_location, pain_notes=EXCLUDED.pain_notes,"
        " pt_done=EXCLUDED.pt_done, habits=EXCLUDED.habits,"
        " day_score=EXCLUDED.day_score",
        (user_id, notion.day, notion.pain_level, notion.pain_location, notion.pain_notes,
         notion.pt_done, json.dumps(notion.habits), notion.day_score),
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
    """Persist today's Garmin + Notion state so query_history has fresh rows."""
    from jim.tools.garmin import get_exercise_sets, get_garmin_today
    from jim.tools.notion import get_notion_logs

    today = _today_for_user(user_id)
    garmin = get_garmin_today(user_id, today)
    # Notion is optional per-user (not everyone shares a knee log) — unlike
    # Garmin, a missing/broken connection here must not crash the whole run.
    try:
        notion = get_notion_logs(user_id, today)
    except Exception:
        log.warning("notion sync unavailable for user %s this run", user_id, exc_info=True)
        notion = None

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
        if notion is not None:
            store_notion_log(conn, user_id, notion)
        conn.commit()


def cleanup_stale_adaptations(user_id: int, today: date) -> None:
    """Delete one-off Garmin workouts (built for a single adapted day, never
    promoted into the playbook) whose day has already passed — see the
    "jim_created_workouts" kv entry written by coach._push_one. A failed
    delete just leaves the entry for tomorrow's sweep to retry."""
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


def _run_nightly_for_user(user_id: int) -> dict:
    """Sync today's data, close today's loop, then plan tomorrow, for `user_id`.

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
        # Cleanup is housekeeping, not the plan itself — a Garmin hiccup here
        # must not stop tonight's session from being planned.
        log.warning("adaptation cleanup failed for user %s", user_id, exc_info=True)
    # Close today's loop first (session is done by 21:00), then plan tomorrow.
    reconcile_day(user_id, today)
    report = run_agent(user_id, today)
    elapsed = round(time.monotonic() - started, 1)
    log.info("nightly done in %ss: %s", elapsed, report)
    return {
        "for_date": report.for_date.isoformat(),
        "suggestion_id": report.suggestion_id,
        "tier": report.tier,
        "research_used": report.research_used,
        "tool_calls": report.tool_calls,
        "fell_back": report.fell_back,
        "elapsed_sec": elapsed,
    }


def run_nightly() -> dict:
    """Fan out the nightly run over every nightly_enabled user.

    One user's failure (expired Garmin creds, Notion down despite the guard in
    sync_today, an unhandled compose/validate error) is caught and logged right
    here, at the per-user boundary — it must not stop the rest of the cron run.

    Cost note: MAX_TOOL_CALLS / model-tier budgets (config.py) were sized for a
    single run. With N nightly_enabled users this loop makes the nightly LLM
    spend scale to N x per cron firing — fine at small N, worth revisiting if
    the user base grows.
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
