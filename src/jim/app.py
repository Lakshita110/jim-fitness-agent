"""Thin FastAPI service (PLAN.md §5): health check and nightly housekeeping cron.

Deployed on Vercel as a single serverless function (api/index.py), so the nightly
job is exposed here as /api/cron/nightly for Vercel Cron to ping, and migrations
are ensured on the request path rather than at startup (see db.ensure_migrated).

Routes live in jim.web.*_routes, grouped by concern (auth, chat, playbook,
garmin onboarding); this module wires them together plus health/cron, too small
to warrant their own file. Also mounts the Garmin MCP server (mcp_server.py) at
/mcp — that's the actual coach surface now; the JSON routes below it are for
auth/settings/legacy chat, no HTML anywhere."""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from jim.config import settings
from jim.mcp_server import build_asgi_app
from jim.web import auth_routes, chat_routes, constraints_routes, garmin_routes, playbook_routes

log = logging.getLogger(__name__)

mcp_app = build_asgi_app()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Apply migrations on boot when there IS a boot (uvicorn locally), and run
    the mounted MCP app's own lifespan (it needs its session-manager task group
    initialized, same as any Starlette sub-app mounted with `app.mount`).

    Serverless has no reliable startup hook — Vercel's ASGI adapter may never run
    this — so the request path calls db.ensure_migrated() too. Both funnel into
    the same once-per-process guard, so whichever fires first wins.

    A migrations failure here is logged, not fatal: /health must still answer
    rather than crash-loop the service while the DB is briefly unreachable.
    """
    try:
        from jim.db import ensure_migrated

        ensure_migrated()
    except Exception:
        log.exception("startup migrations failed — will retry on first request")
    async with mcp_app.lifespan(mcp_app):
        yield


app = FastAPI(title="jim", lifespan=lifespan)
app.mount("/mcp", mcp_app)
app.include_router(auth_routes.router)
app.include_router(chat_routes.router)
app.include_router(playbook_routes.router)
app.include_router(garmin_routes.router)
app.include_router(constraints_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/debug/env")
def debug_env(request: Request) -> dict:
    """TEMPORARY — diagnosing a live "CREDENTIAL_ENCRYPTION_KEY is not set"
    failure on the Vercel deploy even after the variable was added/recreated
    and the app redeployed. Reports only presence/length, never values, and
    is gated behind a real session so it isn't public. Remove once resolved.
    """
    from jim.web import deps

    deps._require_user(request)
    s = settings()
    key = s.credential_encryption_key
    decoded_len = None
    decode_error = None
    if key:
        import base64

        try:
            decoded_len = len(base64.b64decode(key))
        except Exception as e:
            decode_error = str(e)
    from jim.db import connect

    with connect() as conn:
        pk_cols = conn.execute(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'kv'::regclass AND i.indisprimary"
            " ORDER BY a.attnum"
        ).fetchall()
        null_user_id_counts = {}
        for table in ("kv", "garmin_daily", "garmin_activities", "exercise_sets"):
            row = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE user_id IS NULL"
            ).fetchone()
            null_user_id_counts[table] = row["n"]

    return {
        "credential_encryption_key_set": bool(key),
        "credential_encryption_key_raw_len": len(key) if key else 0,
        "credential_encryption_key_decoded_len": decoded_len,
        "credential_encryption_key_decode_error": decode_error,
        "database_url_set": bool(s.database_url),
        "session_secret_set": bool(s.session_secret),
        "cron_secret_set": bool(s.cron_secret),
        "kv_primary_key_columns": [r["attname"] for r in pk_cols],
        "null_user_id_row_counts": null_user_id_counts,
    }


@app.post("/api/debug/delete_orphaned_rows")
def debug_delete_orphaned_rows(request: Request) -> dict:
    """TEMPORARY — one-off cleanup. Pre-multi-tenant rows with user_id IS
    NULL are blocking migration 008_user_pks.sql from promoting kv's (and
    others') primary key to (user_id, ...), which is why kv upserts fail
    with "no unique or exclusion constraint matching the ON CONFLICT
    specification". Garmin remains the source of truth for this data
    regardless, so deleting the orphaned rows (rather than backfilling them
    onto an account) is safe — sync repopulates going forward. Remove this
    endpoint once run.
    """
    from jim.db import connect
    from jim.web import deps

    deps._require_user(request)
    deleted_counts = {}
    with connect() as conn:
        for table in ("kv", "garmin_daily", "garmin_activities", "exercise_sets"):
            row = conn.execute(
                f"DELETE FROM {table} WHERE user_id IS NULL RETURNING 1"
            ).fetchall()
            deleted_counts[table] = len(row)
        conn.commit()
    return {"deleted_counts": deleted_counts}


@app.get("/api/cron/nightly")
def cron_nightly(request: Request) -> dict:
    """The nightly housekeeping run, invoked by Vercel Cron (schedule in vercel.json).

    Vercel authenticates scheduled invocations with `Authorization: Bearer
    $CRON_SECRET`. Without a configured secret this endpoint stays shut — an open
    one would let anyone trigger Garmin syncs for every account on demand.
    """
    secret = settings().cron_secret
    header = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(header, f"Bearer {secret}"):
        raise HTTPException(status_code=403, detail="bad or missing cron secret")

    from jim.jobs.nightly import run_nightly

    result = run_nightly()
    log.info(
        "cron nightly finished in %ss over %d user(s)",
        result.get("elapsed_sec"), len(result.get("users", {})),
    )
    return result
