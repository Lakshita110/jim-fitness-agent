"""M2 backfill: pull ~90 days of Garmin daily metrics + activities into
Postgres so query_history has a real window. Idempotent (upserts).

    python scripts/backfill.py [days] [--email EMAIL]

Defaults to the first-created user (the original single-tenant behavior)
when --email is omitted, so existing invocations keep working unchanged.
"""

import argparse
import logging
from datetime import date, timedelta

from jim.auth import first_user_id, get_user_by_email
from jim.db import connect, migrate
from jim.jobs.nightly import STRENGTH_TYPES, store_exercise_sets
from jim.tools.garmin import get_exercise_sets, get_garmin_today

log = logging.getLogger(__name__)


def main(days: int = 90, email: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    today = date.today()
    with connect() as conn:
        migrate(conn)
        if email:
            user = get_user_by_email(email)
            user_id = user.id if user else None
            if user_id is None:
                raise SystemExit(f"no user found with email {email!r}")
        else:
            user_id = first_user_id()
        if user_id is None:
            raise SystemExit("no users in the database — run scripts/backfill_users.py first")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", nargs="?", type=int, default=90)
    parser.add_argument("--email", help="user to backfill (defaults to the first-created user)")
    args = parser.parse_args()
    main(args.days, args.email)
