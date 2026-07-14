"""M2 backfill: pull ~90 days of Garmin daily metrics + activities, plus the
Notion knee/habit log, into Postgres so query_history has a real window.
Idempotent (upserts).

    python scripts/backfill.py [days]
"""

import logging
import sys
from datetime import date, timedelta

from jim.auth import first_user_id
from jim.db import connect, migrate
from jim.jobs.nightly import STRENGTH_TYPES, store_exercise_sets, store_notion_log
from jim.tools.garmin import get_exercise_sets, get_garmin_today
from jim.tools.notion import get_notion_logs_range

log = logging.getLogger(__name__)


def backfill_notion(conn, user_id: int, start: date, end: date) -> int:
    """Pull the whole window's knee/habit log in one paged range query.

    Pain level/location/notes are what Jim actually plans around, so a Garmin-only
    backfill leaves the history blind to every past flare-up.
    """
    days = get_notion_logs_range(user_id, start, end)
    for notion in days:
        store_notion_log(conn, user_id, notion)
    conn.commit()
    return len(days)


def main(days: int = 90) -> None:
    logging.basicConfig(level=logging.INFO)
    today = date.today()
    with connect() as conn:
        migrate(conn)
        user_id = first_user_id()
        if user_id is None:
            raise SystemExit("no users in the database — run scripts/backfill_users.py first")
        n = backfill_notion(conn, user_id, today - timedelta(days=days), today)
        log.info("backfilled %d notion log days", n)
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            snapshot = get_garmin_today(user_id, day)
            conn.execute(
                "INSERT INTO garmin_daily (user_id, day, hrv, sleep_hours, body_battery,"
                " readiness, resting_hr, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (user_id, day) DO UPDATE SET hrv=EXCLUDED.hrv,"
                " sleep_hours=EXCLUDED.sleep_hours, body_battery=EXCLUDED.body_battery,"
                " readiness=EXCLUDED.readiness, resting_hr=EXCLUDED.resting_hr,"
                " raw=EXCLUDED.raw",
                (user_id, day, snapshot.hrv, snapshot.sleep_hours, snapshot.body_battery,
                 snapshot.readiness, snapshot.resting_hr, snapshot.model_dump_json()),
            )
            for act in snapshot.activities:
                conn.execute(
                    "INSERT INTO garmin_activities (user_id, activity_id, day, type,"
                    " duration_min, training_load, summary)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (user_id, activity_id) DO NOTHING",
                    (user_id, act.activity_id, day, act.type, act.duration_min,
                     act.training_load, act.model_dump_json()),
                )
                if act.type in STRENGTH_TYPES:
                    try:
                        store_exercise_sets(
                            conn, user_id, act.activity_id, day,
                            get_exercise_sets(user_id, act.activity_id),
                        )
                    except Exception:
                        log.exception("sets fetch failed for %s", act.activity_id)
            conn.commit()
            log.info("backfilled %s (%d activities)", day, len(snapshot.activities))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 90)
