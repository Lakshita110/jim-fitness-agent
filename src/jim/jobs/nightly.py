"""Nightly entrypoint for Render Cron (~21:00 local): sync today's data into
Postgres, then run the agent. `python -m jim.jobs.nightly`."""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from jim.agent.loop import run_agent
from jim.config import settings
from jim.db import connect, migrate

log = logging.getLogger(__name__)


STRENGTH_TYPES = ("strength_training", "fitness_equipment")


def store_exercise_sets(conn, activity_id: str, day, sets: list[dict]) -> None:
    """Upsert an activity's ACTIVE sets (per-exercise reps/weights)."""
    for s in sets:
        conn.execute(
            "INSERT INTO exercise_sets (activity_id, set_index, day, category,"
            " exercise_name, reps, weight_kg, duration_sec)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (activity_id, set_index) DO NOTHING",
            (activity_id, s["set_index"], day, s.get("category"),
             s.get("exercise_name"), s.get("reps"), s.get("weight_kg"),
             s.get("duration_sec")),
        )


def sync_today() -> None:
    """Persist today's Garmin + Notion state so query_history has fresh rows."""
    from jim.tools.garmin import get_exercise_sets, get_garmin_today
    from jim.tools.notion import get_notion_logs

    today = datetime.now(ZoneInfo(settings().app_timezone)).date()
    garmin = get_garmin_today(today)
    notion = get_notion_logs(today)

    with connect() as conn:
        conn.execute(
            "INSERT INTO garmin_daily (day, hrv, sleep_hours, body_battery, readiness,"
            " resting_hr, raw) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (day) DO UPDATE SET hrv=EXCLUDED.hrv,"
            " sleep_hours=EXCLUDED.sleep_hours, body_battery=EXCLUDED.body_battery,"
            " readiness=EXCLUDED.readiness, resting_hr=EXCLUDED.resting_hr, raw=EXCLUDED.raw",
            (today, garmin.hrv, garmin.sleep_hours, garmin.body_battery,
             garmin.readiness, garmin.resting_hr, garmin.model_dump_json()),
        )
        for act in garmin.activities:
            conn.execute(
                "INSERT INTO garmin_activities (activity_id, day, type, duration_min,"
                " training_load, summary) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (activity_id) DO NOTHING",
                (act.activity_id, today, act.type, act.duration_min,
                 act.training_load, act.model_dump_json()),
            )
            if act.type in STRENGTH_TYPES:
                try:
                    store_exercise_sets(
                        conn, act.activity_id, today, get_exercise_sets(act.activity_id)
                    )
                except Exception:
                    log.exception("exercise sets fetch failed for %s", act.activity_id)
        conn.execute(
            "INSERT INTO notion_daily_log (day, pain_level, pain_location, pt_done,"
            " habits, day_score) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (day) DO UPDATE SET pain_level=EXCLUDED.pain_level,"
            " pain_location=EXCLUDED.pain_location, pt_done=EXCLUDED.pt_done,"
            " habits=EXCLUDED.habits, day_score=EXCLUDED.day_score",
            (today, notion.pain_level, notion.pain_location, notion.pt_done,
             json.dumps(notion.habits), notion.day_score),
        )
        conn.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from jim.jobs.reconcile import reconcile_day

    with connect() as conn:
        migrate(conn)
    sync_today()
    today = datetime.now(ZoneInfo(settings().app_timezone)).date()
    # Close today's loop first (session is done by 21:00), then plan tomorrow.
    reconcile_day(today)
    report = run_agent(today)
    log.info("nightly done: %s", report)


if __name__ == "__main__":
    main()
