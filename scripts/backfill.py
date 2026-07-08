"""M2 backfill: pull ~90 days of Garmin daily metrics + activities into
Postgres so query_history has a real window. Idempotent (upserts).

    python scripts/backfill.py [days]
"""

import logging
import sys
from datetime import date, timedelta

from jim.db import connect, migrate
from jim.jobs.nightly import STRENGTH_TYPES, store_exercise_sets
from jim.tools.garmin import get_exercise_sets, get_garmin_today

log = logging.getLogger(__name__)


def main(days: int = 90) -> None:
    logging.basicConfig(level=logging.INFO)
    with connect() as conn:
        migrate(conn)
        for offset in range(days, -1, -1):
            day = date.today() - timedelta(days=offset)
            snapshot = get_garmin_today(day)
            conn.execute(
                "INSERT INTO garmin_daily (day, hrv, sleep_hours, body_battery,"
                " readiness, resting_hr, raw) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (day) DO UPDATE SET hrv=EXCLUDED.hrv,"
                " sleep_hours=EXCLUDED.sleep_hours, body_battery=EXCLUDED.body_battery,"
                " readiness=EXCLUDED.readiness, resting_hr=EXCLUDED.resting_hr,"
                " raw=EXCLUDED.raw",
                (day, snapshot.hrv, snapshot.sleep_hours, snapshot.body_battery,
                 snapshot.readiness, snapshot.resting_hr, snapshot.model_dump_json()),
            )
            for act in snapshot.activities:
                conn.execute(
                    "INSERT INTO garmin_activities (activity_id, day, type,"
                    " duration_min, training_load, summary)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (activity_id) DO NOTHING",
                    (act.activity_id, day, act.type, act.duration_min,
                     act.training_load, act.model_dump_json()),
                )
                if act.type in STRENGTH_TYPES:
                    try:
                        store_exercise_sets(
                            conn, act.activity_id, day, get_exercise_sets(act.activity_id)
                        )
                    except Exception:
                        log.exception("sets fetch failed for %s", act.activity_id)
            conn.commit()
            log.info("backfilled %s (%d activities)", day, len(snapshot.activities))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 90)
