"""Thin Postgres layer: connections + additive migration runner. Raw SQL only —
the deterministic feature computation lives in tools/history.py as pure
functions so it can be unit-tested without a database."""

import json
import logging
import threading
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from jim.config import settings

log = logging.getLogger(__name__)

# Inside the package, not the repo root: a serverless bundle (and any non-editable
# install) ships package data but not loose top-level directories.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_migrated = False
_migrate_lock = threading.Lock()


def connect() -> psycopg.Connection:
    url = settings().database_url
    if not url:
        # Otherwise psycopg tries to reach a local socket and every DB-backed
        # route returns a bare 500, with nothing in the logs saying why. The app
        # boots and authenticates fine without a database, so this is easy to
        # misread as a code fault rather than a missing env var.
        raise RuntimeError(
            "DATABASE_URL is not set — the app cannot reach Postgres. On Vercel,"
            " env vars only apply to deployments created after they were added,"
            " so add it and redeploy."
        )
    return psycopg.connect(url, row_factory=dict_row)


def ensure_migrated() -> None:
    """Apply migrations once per process.

    Serverless can't rely on a startup hook — Vercel's ASGI adapter does not
    reliably run FastAPI's lifespan, and a fresh deploy with no tables 500s on
    every request. So the request path ensures the schema itself; after the first
    call this is a boolean check.
    """
    global _migrated
    if _migrated:
        return
    with _migrate_lock:
        if _migrated:
            return
        with connect() as conn:
            migrate(conn)
            conn.commit()
        _migrated = True
        log.info("migrations applied")


def kv_get(user_id: int, key: str) -> Any:
    """Read a value from the kv store (None if absent), scoped to `user_id`."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM kv WHERE user_id = %s AND key = %s", (user_id, key)
        ).fetchone()
    return row["value"] if row else None


def kv_set(user_id: int, key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (user_id, key, value, updated_ts) VALUES (%s, %s, %s, now())"
            " ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_ts = now()",
            (user_id, key, json.dumps(value)),
        )
        conn.commit()


_CRED_FIELDS = ("garmin_password", "garmin_tokens", "notion_token")


def get_user_credentials(user_id: int) -> dict | None:
    """Decrypted credentials for `user_id`, or None if no row exists (shouldn't
    happen post-signup, but callers should be defensive)."""
    from jim import crypto

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_credentials WHERE user_id = %s", (user_id,)
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["garmin_password"] = (
        crypto.decrypt(row["garmin_password_enc"]) if row.get("garmin_password_enc") else None
    )
    out["garmin_tokens"] = (
        crypto.decrypt(row["garmin_tokens_enc"]) if row.get("garmin_tokens_enc") else None
    )
    out["notion_token"] = (
        crypto.decrypt(row["notion_token_enc"]) if row.get("notion_token_enc") else None
    )
    return out


def save_user_credentials(user_id: int, **fields: Any) -> None:
    """Upsert whichever credential fields are passed, encrypting the
    plaintext-named ones (`garmin_password`, `garmin_tokens`, `notion_token`)
    into their `_enc` columns. Other fields (`garmin_email`,
    `notion_knee_log_db_id`) are stored as-is."""
    from jim import crypto

    cols: list[str] = []
    values: list[Any] = []
    for name, value in fields.items():
        if name in _CRED_FIELDS:
            cols.append(f"{name}_enc")
            values.append(crypto.encrypt(value) if value else None)
        else:
            cols.append(name)
            values.append(value)
    if not cols:
        return
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    with connect() as conn:
        conn.execute(
            f"INSERT INTO user_credentials (user_id, {', '.join(cols)})"
            f" VALUES (%s, {', '.join(['%s'] * len(cols))})"
            f" ON CONFLICT (user_id) DO UPDATE SET {set_clause}, updated_ts = now()",
            (user_id, *values, *values),
        )
        conn.commit()


def migrate(conn: psycopg.Connection) -> None:
    """Apply every migrations/*.sql in name order. Files are idempotent, so we
    simply re-run them all — no version table needed while the set is small.

    A missing pgvector extension only disables the research corpus (M4), so
    that failure is downgraded to a warning instead of blocking the nightly run.

    008_user_pks.sql's composite-PK promotion fails loudly (NotNullViolation)
    on any table still carrying legacy user_id-less rows — by design, until
    scripts/backfill_users.py has run. That's expected on a freshly-deployed
    multi-tenant build before the operator has backfilled the existing athlete's
    data, and it must not crash-loop the whole app in the meantime (every
    DB-backed route calls ensure_migrated() on the request path) — so it's
    downgraded to a warning too. It applies cleanly, permanently, the first
    migrate() call after the backfill has run."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        log.info("applying migration %s", path.name)
        try:
            conn.execute(path.read_text())
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            if 'extension "vector"' in str(e):
                log.warning("skipping %s (pgvector unavailable): %s", path.name, e)
                continue
            if isinstance(e, psycopg.errors.NotNullViolation) and "user_id" in str(e):
                log.warning(
                    "skipping %s (user_id backfill not done yet): %s", path.name, e
                )
                continue
            raise
