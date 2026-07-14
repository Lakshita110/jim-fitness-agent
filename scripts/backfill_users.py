"""One-off (soft-baking-kettle plan, Phase 2 backfill): create the existing
athlete's `users` row and populate their `user_credentials`/`playbooks` rows
from what's already in the environment/committed files, so the one real user
isn't locked out once the CHAT_SECRET auth path is removed.

    python scripts/backfill_users.py [email] [--password PW]

Prompts for whatever isn't supplied on the command line. Not idempotent by
design — this is a true one-off; re-running with the same email fails loudly
rather than silently duplicating the account.
"""

import argparse
import getpass
import json
import sys

from jim import auth, crypto
from jim.config import settings
from jim.db import connect, migrate
from jim.playbook import _load_playbook_from_disk

# Tables carrying a nullable user_id (007_users.sql) that must be backfilled
# before 008_user_pks.sql can promote it into a composite primary key.
BACKFILL_TABLES = (
    "garmin_daily", "garmin_activities", "notion_daily_log", "features_daily",
    "suggestions", "outcomes", "exercise_sets", "kv",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", help="login email for the new account")
    parser.add_argument("--password", help="login password (prompted if omitted)")
    args = parser.parse_args()

    email = args.email or input("Login email for this account: ").strip()
    password = args.password or getpass.getpass("Login password: ")
    if not email or not password:
        print("email and password are both required", file=sys.stderr)
        sys.exit(1)

    with connect() as conn:
        migrate(conn)
        conn.commit()

    try:
        user = auth.create_user(email, password)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    s = settings()
    with connect() as conn:
        conn.execute(
            "UPDATE user_credentials SET garmin_email = %s, garmin_password_enc = %s,"
            " garmin_tokens_enc = %s, notion_token_enc = %s, notion_knee_log_db_id = %s,"
            " updated_ts = now() WHERE user_id = %s",
            (
                s.garmin_email or None,
                crypto.encrypt(s.garmin_password) if s.garmin_password else None,
                crypto.encrypt(s.garmin_tokens) if s.garmin_tokens else None,
                crypto.encrypt(s.notion_token) if s.notion_token else None,
                s.notion_knee_log_db_id or None,
                user.id,
            ),
        )

        pb = _load_playbook_from_disk()
        conn.execute(
            "UPDATE playbooks SET rotation = %s, workouts = %s, pt_routines = %s,"
            " directives = %s, updated_ts = now() WHERE user_id = %s",
            (
                json.dumps(pb.rotation),
                json.dumps({k: v.model_dump(mode="json") for k, v in pb.workouts.items()}),
                json.dumps({k: v.model_dump(mode="json") for k, v in pb.pt_routines.items()}),
                pb.directives,
                user.id,
            ),
        )
        conn.commit()

    # Backfill user_id onto this athlete's existing historical rows so
    # 008_user_pks.sql can safely promote user_id into a composite PK — a
    # composite PK can't contain NULLs, and Postgres will refuse (loudly) if
    # this step is skipped. Not idempotent across users by design: only rows
    # still NULL are claimed, so re-running after a second user exists won't
    # steal their rows.
    with connect() as conn:
        for table in BACKFILL_TABLES:
            conn.execute(
                f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL",  # noqa: S608
                (user.id,),
            )
        conn.commit()

    print(f"Created user #{user.id} <{user.email}>.")
    print(
        "  credentials: "
        + ", ".join(
            name
            for name, present in (
                ("garmin_email", bool(s.garmin_email)),
                ("garmin_password", bool(s.garmin_password)),
                ("garmin_tokens", bool(s.garmin_tokens)),
                ("notion_token", bool(s.notion_token)),
            )
            if present
        )
        or "  credentials: none found in env"
    )
    print(f"  playbook: {len(pb.rotation)} rotation slots, {len(pb.workouts)} workouts,"
          f" {len(pb.pt_routines)} PT routines, "
          f"{'directives set' if pb.directives else 'no directives'}")
    print("Sign in at /login with the email/password you just entered.")


if __name__ == "__main__":
    main()
