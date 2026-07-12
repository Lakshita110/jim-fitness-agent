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
    return psycopg.connect(settings().database_url, row_factory=dict_row)


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


def kv_get(key: str) -> Any:
    """Read a value from the kv store (None if absent)."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_ts) VALUES (%s, %s, now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_ts = now()",
            (key, json.dumps(value)),
        )
        conn.commit()


def migrate(conn: psycopg.Connection) -> None:
    """Apply every migrations/*.sql in name order. Files are idempotent, so we
    simply re-run them all — no version table needed while the set is small.

    A missing pgvector extension only disables the research corpus (M4), so
    that failure is downgraded to a warning instead of blocking the nightly run."""
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
            raise
