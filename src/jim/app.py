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
