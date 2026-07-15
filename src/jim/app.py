"""Thin FastAPI service (PLAN.md §5): health check, nightly housekeeping cron, and
Jim's chat — a private, mobile-friendly page (add to home screen) where the
athlete iterates on plans and pushes them to Garmin on approve.

Deployed on Vercel as a single serverless function (api/index.py), so the nightly
job is exposed here as /api/cron/nightly for Vercel Cron to ping, and migrations
are ensured on the request path rather than at startup (see db.ensure_migrated).

Routes live in jim.web.*_routes, grouped by concern (auth, chat, playbook,
garmin onboarding); this module wires them together plus the handful of routes
(health, cron, static/manifest, /login) too small to warrant their own file."""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from jim.config import settings
from jim.web import auth_routes, chat_routes, garmin_routes, playbook_routes
from jim.web.templates import LOGIN_PAGE

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_SIZES = (180, 192, 512)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Apply migrations on boot when there IS a boot (uvicorn locally).

    Serverless has no reliable startup hook — Vercel's ASGI adapter may never run
    this — so the request path calls db.ensure_migrated() too. Both funnel into
    the same once-per-process guard, so whichever fires first wins.

    A failure here is logged, not fatal: /health must still answer rather than
    crash-loop the service while the DB is briefly unreachable.
    """
    try:
        from jim.db import ensure_migrated

        ensure_migrated()
    except Exception:
        log.exception("startup migrations failed — will retry on first request")
    yield


app = FastAPI(title="jim", lifespan=lifespan)
app.include_router(auth_routes.router)
app.include_router(chat_routes.router)
app.include_router(playbook_routes.router)
app.include_router(garmin_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/cron/nightly")
def cron_nightly(request: Request) -> dict:
    """The nightly housekeeping run, invoked by Vercel Cron (schedule in vercel.json).

    Vercel authenticates scheduled invocations with `Authorization: Bearer
    $CRON_SECRET`. Without a configured secret this endpoint stays shut — an open
    one would let anyone trigger Garmin/Notion syncs for every account on demand.
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


# --- home-screen install (DEPLOY.md) -----------------------------------------
# Neither the icons nor the manifest carry a secret any more (start_url is the
# bare /chat, authenticated by cookie), so both are public. That also means the
# browser can fetch them during install without needing to send credentials.


@app.get("/icon-{size}.png")
def icon(size: int) -> Response:
    if size not in ICON_SIZES:
        raise HTTPException(status_code=404, detail="no icon at that size")
    return Response(
        (STATIC_DIR / f"icon-{size}.png").read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    # No key anywhere in here — start_url is the clean /chat and the installed app
    # authenticates with its session cookie. Nothing secret, so it needs no gate.
    return JSONResponse(
        {
            "name": "Jim — training coach",
            "short_name": "Jim",
            "description": "Your training partner in crime.",
            "start_url": "/chat",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#0F100D",
            "theme_color": "#0F100D",
            "icons": [
                {"src": f"/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png",
                 "purpose": "any maskable"}
                for s in ICON_SIZES
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/login")
def login_page() -> Response:
    return HTMLResponse(LOGIN_PAGE)
