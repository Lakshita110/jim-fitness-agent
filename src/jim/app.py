"""Thin FastAPI service (PLAN.md §5): health check, manual nightly trigger, and
Jim's chat — a private, mobile-friendly page (add to home screen) where the
athlete iterates on plans and pushes them to Garmin on approve.

Deployed on Vercel as a single serverless function (api/index.py), so the nightly
job is exposed here as /api/cron/nightly for Vercel Cron to ping, and migrations
are ensured on the request path rather than at startup (see db.ensure_migrated)."""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from jim import coach
from jim.agent.loop import run_agent
from jim.config import settings

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


def _ready() -> None:
    """Schema is present before we touch the DB. No-op after the first call."""
    from jim.db import ensure_migrated

    ensure_migrated()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def trigger_run() -> dict:
    """Manual agent run (plan only — no data sync). See /api/cron/nightly."""
    _ready()
    today = datetime.now(ZoneInfo(settings().app_timezone)).date()
    report = run_agent(today)
    return {
        "for_date": report.for_date.isoformat(),
        "suggestion_id": report.suggestion_id,
        "tier": report.tier,
        "research_used": report.research_used,
        "tool_calls": report.tool_calls,
        "fell_back": report.fell_back,
    }


@app.get("/api/cron/nightly")
def cron_nightly(request: Request) -> dict:
    """The nightly run, invoked by Vercel Cron (schedule in vercel.json).

    Vercel authenticates scheduled invocations with `Authorization: Bearer
    $CRON_SECRET`. Without a configured secret this endpoint stays shut — an open
    one would let anyone burn LLM spend and rewrite tomorrow's plan.
    """
    secret = settings().cron_secret
    header = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(header, f"Bearer {secret}"):
        raise HTTPException(status_code=403, detail="bad or missing cron secret")

    from jim.jobs.nightly import run_nightly

    result = run_nightly()
    log.info("cron nightly finished in %ss", result.get("elapsed_sec"))
    return result


# --- Jim's chat ---------------------------------------------------------------
#
# Auth is one shared key, presented either way:
#   1. ?key=<CHAT_SECRET> — the sign-in. Visiting /chat with it sets a cookie and
#      redirects to a clean /chat.
#   2. the session cookie — everything after that.
#
# The cookie is why the key doesn't have to live in the URL: out of browser
# history, out of the PWA manifest, and out of any screenshot of the address bar.
# It holds a token DERIVED from the secret, not the secret itself, so reading the
# cookie jar doesn't hand over the shareable key.

SESSION_COOKIE = "jim_session"
SESSION_MAX_AGE = 400 * 24 * 3600  # ~13 months (Chrome caps cookie life at 400d)


def _session_token() -> str:
    secret = settings().chat_secret
    return hmac.new(secret.encode(), b"jim-session-v1", "sha256").hexdigest()


def _authed(request: Request, key: str = "") -> bool:
    """Valid key in the request, or a valid session cookie."""
    secret = settings().chat_secret
    if not secret:  # chat stays disabled until a secret is configured
        return False
    if key and hmac.compare_digest(key, secret):
        return True
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, _session_token())


def _require_auth(request: Request, key: str = "") -> None:
    if not _authed(request, key):
        raise HTTPException(status_code=403, detail="bad or missing chat key")


def _set_session(response: Response, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,   # JS can't read it, so an XSS can't exfiltrate the session
        secure=secure,   # https only in prod; must be off on plain-http localhost
        samesite="lax",
    )


class KeyOnly(BaseModel):
    key: str = ""


class ChatMessage(BaseModel):
    key: str = ""
    text: str
    scope_date: str | None = None  # ISO date: edit only this day


class PushDay(BaseModel):
    key: str = ""
    date: str  # ISO date of the draft day to push/update


@app.post("/chat/message")
def chat_message(msg: ChatMessage, request: Request) -> dict:
    _require_auth(request, msg.key)
    if not msg.text.strip():
        raise HTTPException(status_code=400, detail="empty message")
    _ready()
    return coach.converse(msg.text.strip(), scope_date=msg.scope_date)


@app.post("/chat/approve")
def chat_approve(body: KeyOnly, request: Request) -> dict:
    _require_auth(request, body.key)
    _ready()
    summary = coach.approve()
    state = coach.current_state()
    return {"summary": summary, "draft": state["draft"],
            "push_status": state["push_status"]}


@app.post("/chat/push-day")
def chat_push_day(body: PushDay, request: Request) -> dict:
    _require_auth(request, body.key)
    _ready()
    return coach.push_day(body.date)


@app.post("/chat/clear")
def chat_clear(body: KeyOnly, request: Request) -> dict:
    _require_auth(request, body.key)
    _ready()
    coach.clear()
    return {"ok": True}


@app.get("/chat/state")
def chat_state(request: Request, key: str = "") -> dict:
    _require_auth(request, key)
    _ready()
    return coach.current_state()


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


CHAT_PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0F100D">
<title>Jim</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="icon" type="image/png" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Jim">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,500;1,9..144,600&family=Inter:wght@400;450;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#0F100D; --glass:rgba(255,255,255,.045); --glass-line:rgba(255,255,255,.09);
  --solid:#171812; --line:rgba(255,255,255,.08);
  --ink:#F2EFE7; --muted:#9A9C90;
  --sage:#B4CE9E; --sage-dim:#8FAE78; --data:#E7B57C; --warn:#D98C7A;
  --bubble-me:#232619; --bubble-bot:#1B1D16;
}
* { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: 'Inter', -apple-system, system-ui, sans-serif;
       background:
         radial-gradient(900px 460px at 10% -8%, rgba(180,206,158,.12) 0%, transparent 58%),
         radial-gradient(800px 420px at 105% 0%, rgba(231,181,124,.10) 0%, transparent 55%),
         radial-gradient(700px 500px at 50% 115%, rgba(217,140,122,.07) 0%, transparent 60%),
         linear-gradient(180deg, #14150F 0%, var(--bg) 45%);
       color: var(--ink); height: 100dvh; display: flex; flex-direction: column;
       -webkit-font-smoothing: antialiased; overflow: hidden; }

header { padding: 16px 22px 10px; display: flex; align-items: center; gap: 12px;
         z-index: 5; flex-shrink: 0; }
.brand { flex: 1; min-width: 0; }
.brand-name { font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
              font-size: 24px; letter-spacing: -.01em; line-height: 1.1; }
.brand-sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; }
#clear { color: var(--muted); font-size: 12px; text-decoration: none; flex-shrink: 0; }
#clear:hover { color: var(--ink); }

.main { flex: 1; display: flex; min-height: 0; position: relative; }
.chat-col { flex: 1 1 58%; min-width: 0; display: flex; flex-direction: column;
            border-right: 1px solid var(--line); }
.plan-col { flex: 0 0 42%; max-width: 400px; min-width: 300px; display: flex;
            flex-direction: column; position: relative; }
.plan-col::after { content:""; position: absolute; left: 0; right: 0; bottom: 0; height: 200px;
                   background: radial-gradient(130% 100% at 50% 130%, rgba(180,206,158,.09), transparent 70%);
                   pointer-events: none; z-index: 0; }

/* --- stat cards ---------------------------------------------------------- */
#cards { display: flex; gap: 10px; padding: 6px 16px 12px; flex-shrink: 0;
         overflow-x: auto; scrollbar-width: none; }
#cards::-webkit-scrollbar { display: none; }
.card { position: relative; min-width: 150px; flex: 0 0 auto; padding: 12px 15px 13px;
        background: var(--glass); border: 1px solid var(--glass-line); border-radius: 16px;
        backdrop-filter: blur(14px); overflow: hidden; }
.card::before { content:""; position: absolute; left: -20%; top: -60%; width: 90%; height: 120%;
                background: radial-gradient(closest-side, var(--glow, transparent), transparent);
                opacity: .5; pointer-events: none; }
.card.ready { --glow: rgba(180,206,158,.28); }
.card.next  { --glow: rgba(231,181,124,.24); }
.card.pain  { --glow: rgba(217,140,122,.26); }
.c-label { display: flex; align-items: center; gap: 6px; font-size: 10.5px; font-weight: 600;
           letter-spacing: .07em; text-transform: uppercase; color: var(--muted); }
.c-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
.card.push .c-dot { background: var(--sage); }
.card.steady .c-dot { background: var(--muted); }
.card.ease .c-dot { background: var(--data); }
.card.rest .c-dot { background: var(--warn); }
.c-main { font-size: 14.5px; font-weight: 550; margin-top: 6px; color: var(--ink);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 210px; }
.c-sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; white-space: nowrap; }
.c-sub .num, .c-main .num { color: var(--data); font-weight: 600; font-variant-numeric: tabular-nums; }

/* --- chat ----------------------------------------------------------------- */
#log { flex: 1; overflow-y: auto; padding: 8px 16px; display: flex;
       flex-direction: column; gap: 11px; }
.hero { margin: auto; align-self: center; text-align: center; max-width: 420px; padding: 12px; }
.hero-hi { font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
           font-size: 30px; letter-spacing: -.01em; }
.hero-line { font-size: 14.5px; color: var(--ink); margin-top: 10px; font-weight: 500; }
.hero-sub { font-size: 12.5px; color: var(--muted); margin-top: 6px; line-height: 1.55; }
.hero-ask { font-size: 10.5px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase;
            color: var(--muted); margin: 22px 0 10px; }
.hero .chips { justify-content: center; }
.row { display: flex; max-width: 86%; gap: 9px; }
.row.me { align-self: flex-end; }
.row.bot { align-self: flex-start; align-items: flex-end; }
.avatar { width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0; margin-bottom: 2px;
          background: radial-gradient(circle at 34% 30%, var(--sage), var(--sage-dim)); }
.msg { padding: 11px 15px; border-radius: 16px; font-size: 14.5px; line-height: 1.55;
       white-space: pre-wrap; word-wrap: break-word; }
.me .msg { background: var(--bubble-me); color: var(--ink); border-bottom-right-radius: 5px; }
.bot .msg { background: var(--bubble-bot); color: var(--ink); border-bottom-left-radius: 5px;
            border: 1px solid rgba(255,255,255,.04); }
.msg strong { font-weight: 700; }
.msg strong.md-h { display: block; margin: 2px 0 5px; font-size: 11px; font-weight: 700;
                    letter-spacing: .07em; text-transform: uppercase; color: var(--data); }
.msg.busy { display: flex; gap: 4px; align-items: center; padding: 14px; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted);
       animation: bounce 1.3s infinite; }
.dot:nth-child(2){ animation-delay:.16s } .dot:nth-child(3){ animation-delay:.32s }
@keyframes bounce { 0%,64%,100%{ transform: translateY(0); opacity:.4 }
                    32%{ transform: translateY(-5px); opacity:1 } }
.chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 2px; }
.chip { display: inline-flex; align-items: center; gap: 7px;
        border: 1px solid var(--glass-line); background: var(--glass); color: var(--ink);
        border-radius: 999px; padding: 9px 15px; font-size: 12.5px; font-weight: 500;
        font-family: inherit; cursor: pointer; backdrop-filter: blur(10px); }
.chip i { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; font-style: normal; }
.chip:hover { border-color: var(--sage); }
.chip:active { transform: scale(.97); }

/* --- composer -------------------------------------------------------------- */
form { padding: 10px 16px calc(14px + env(safe-area-inset-bottom)); flex-shrink: 0; }
.composer { display: flex; align-items: center; gap: 8px; padding: 7px 8px 7px 9px;
            background: var(--glass); border: 1px solid var(--glass-line);
            border-radius: 999px; backdrop-filter: blur(14px); }
.composer:focus-within { border-color: rgba(180,206,158,.5); }
#plus { width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--glass-line);
        background: transparent; color: var(--muted); font-size: 18px; line-height: 1;
        cursor: pointer; flex-shrink: 0; }
#plus:hover { color: var(--ink); }
#t { flex: 1; min-width: 0; border: none; outline: none; background: transparent;
     font-size: 15px; font-family: inherit; font-weight: 450; color: var(--ink); padding: 8px 4px; }
#t::placeholder { color: var(--muted); }
#send { border: none; border-radius: 50%; width: 40px; height: 40px; flex-shrink: 0;
        background: linear-gradient(145deg, var(--sage), var(--sage-dim)); color: #1a2013;
        font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
#send:active { transform: scale(.94); }

/* --- plan panel ------------------------------------------------------------ */
.peek { display: none; }
.plan-head { padding: 16px 20px 10px; flex-shrink: 0; position: relative; z-index: 1; }
.plan-title { font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
              font-size: 19px; letter-spacing: -.01em; color: var(--ink); }
.plan-status { font-size: 11px; letter-spacing: .02em; color: var(--muted); margin-top: 4px; }
.plan-rows { flex: 1; overflow-y: auto; padding: 4px 12px; position: relative; z-index: 1; }
.row-day { position: relative; padding: 13px 14px; min-height: 58px; border-radius: 14px;
           margin-bottom: 2px; }
.row-day.today { background: linear-gradient(100deg, rgba(180,206,158,.12), rgba(180,206,158,.03) 72%); }
.row-day.today::before { content:""; position: absolute; left: 0; top: 13px; bottom: 13px;
                          width: 3px; border-radius: 3px; background: var(--sage); }
.row-day.rest { opacity: .62; }  /* dimmed, but still readable + editable */
.row-day.pulse { animation: rowPulse 1100ms ease-out; }
@keyframes rowPulse { 0% { background: rgba(180,206,158,.22); } 100% { background: transparent; } }
.row-top { display: flex; align-items: baseline; gap: 11px; }
.r-type { font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; font-weight: 600;
          color: var(--muted); width: 34px; flex-shrink: 0; }
.r-title { font-weight: 550; font-size: 15px; color: var(--ink); flex: 1; min-width: 0;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-day.rest .r-title { color: var(--muted); font-weight: 450; }
.r-dur { font-weight: 600; font-size: 13.5px; color: var(--data); flex-shrink: 0;
         font-variant-numeric: tabular-nums; }
.row-sub { display: flex; gap: 10px; margin-top: 4px; padding-left: 45px; font-size: 12px;
           color: var(--muted); overflow: hidden; }
.row-sub .r-date { flex-shrink: 0; letter-spacing: .02em; font-variant-numeric: tabular-nums; }
.row-sub .r-steps { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num { color: var(--data); font-weight: 600; font-variant-numeric: tabular-nums; }
/* subtle scrollbar so the panel doesn't show the default heavy one */
.plan-rows { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.16) transparent; overscroll-behavior: contain; }
.plan-rows::-webkit-scrollbar { width: 8px; }
.plan-rows::-webkit-scrollbar-track { background: transparent; }
.plan-rows::-webkit-scrollbar-thumb { background: rgba(255,255,255,.12); border-radius: 4px; }
.plan-rows::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.20); }
/* click a day to expand its full workout */
.row-day.clickable { cursor: pointer; }
.row-day.clickable:hover { background: rgba(255,255,255,.03); }
.r-chev { color: var(--muted); font-size: 12px; flex-shrink: 0; transition: transform .2s; }
.row-day.open > .row-top .r-chev { transform: rotate(90deg); }
.row-day.open .r-steps { display: none; }
.row-detail { display: none; margin-top: 8px; padding-left: 45px; }
.row-day.open .row-detail { display: block; }
.d-step { font-size: 12.5px; color: var(--ink); padding: 3px 0; line-height: 1.4; }
.d-step .d-note { color: var(--muted); font-size: 11.5px; }
.d-why { font-size: 12px; color: var(--muted); font-style: italic; margin-top: 8px;
         padding-top: 8px; border-top: 1px solid var(--line); }
/* per-day status badge (in the row sub-line) */
.r-badge { flex-shrink: 0; font-size: 10px; font-weight: 600; letter-spacing: .04em;
           text-transform: uppercase; padding: 1px 7px; border-radius: 999px; }
.r-badge.on  { color: var(--sage); background: rgba(180,206,158,.12); border: 1px solid rgba(180,206,158,.3); }
.r-badge.mod { color: var(--data); background: rgba(231,181,124,.12); border: 1px solid rgba(231,181,124,.3); }
/* inline edit affordance on a day row */
.r-edit { flex-shrink: 0; border: none; background: transparent; color: var(--muted);
          font-size: 13px; cursor: pointer; padding: 2px 4px; line-height: 1; }
.r-edit:hover { color: var(--sage); }
/* action bar inside an expanded day */
.d-actions { display: flex; gap: 8px; margin-top: 10px; }
.d-btn { flex: 1; padding: 8px; border-radius: 10px; font-family: inherit; font-size: 12px;
         font-weight: 600; cursor: pointer; border: 1px solid var(--glass-line); background: var(--glass); color: var(--ink); }
.d-btn.push { border-color: var(--sage); color: var(--sage); }
.d-btn.push:hover { background: rgba(180,206,158,.08); }
.d-btn:active { transform: scale(.98); }
/* scope pill above the composer */
.scope-bar { display: flex; padding: 0 2px 8px; }
.scope-pill { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 500;
              color: var(--sage); background: rgba(180,206,158,.10); border: 1px solid rgba(180,206,158,.32);
              border-radius: 999px; padding: 6px 8px 6px 13px; }
.scope-pill button { border: none; background: transparent; color: var(--sage); font-size: 15px;
                     line-height: 1; cursor: pointer; padding: 0 2px; }
.plan-foot { padding: 14px 18px calc(16px + env(safe-area-inset-bottom));
             flex-shrink: 0; position: relative; z-index: 1; }
#push { width: 100%; padding: 14px; background: transparent; border: 1.5px solid var(--sage);
        border-radius: 999px; color: var(--sage); font-weight: 600; font-size: 13.5px;
        font-family: inherit; letter-spacing: .01em; cursor: pointer; }
#push:hover:not(:disabled) { background: rgba(180,206,158,.08); }
#push:active:not(:disabled) { transform: translateY(1px); }
#push.syncing { background: linear-gradient(145deg, var(--sage), var(--sage-dim));
                color: #1a2013; border-color: transparent; }
#push:disabled { opacity: .4; cursor: default; border-color: var(--line); color: var(--muted); }

@media (max-width: 880px) {
  .chat-col { border-right: none; padding-bottom: calc(56px + env(safe-area-inset-bottom)); }
  .plan-col { position: fixed; left: 0; right: 0; bottom: 0; top: auto; height: 82dvh;
              max-width: none; min-width: 0; background: var(--solid);
              border-top: 1px solid var(--line); border-radius: 20px 20px 0 0;
              transform: translateY(calc(100% - 56px));
              transition: transform .28s cubic-bezier(.4,0,.2,1); z-index: 30; }
  .plan-col.expanded { transform: translateY(0); }
  .peek { display: flex; align-items: center; position: relative; height: 56px;
          padding: 0 20px; flex-shrink: 0; cursor: pointer; }
  .peek-handle { position: absolute; left: 50%; top: 8px; transform: translateX(-50%);
                 width: 36px; height: 4px; border-radius: 2px; background: rgba(255,255,255,.16); }
  .peek-text { font-size: 13px; color: var(--ink); flex: 1; padding-top: 6px; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .peek-chev { color: var(--muted); font-size: 11px; padding-top: 6px; flex-shrink: 0;
               transition: transform .28s; }
  .plan-col.expanded .peek-chev { transform: rotate(180deg); }
}
</style></head><body>
<header>
  <div class="brand">
    <div class="brand-name">Jim</div>
    <div class="brand-sub">Your training partner in crime</div>
  </div>
  <a href="#" id="clear">Clear</a>
</header>
<div class="main">
  <div class="chat-col">
    <div id="cards">
      <div class="card ready" id="cardReady" hidden>
        <div class="c-label"><span class="c-dot"></span>Readiness</div>
        <div class="c-main" id="crMain">—</div>
        <div class="c-sub" id="crSub"></div>
      </div>
      <div class="card next" id="cardNext">
        <div class="c-label">Next session</div>
        <div class="c-main" id="cnMain">—</div>
        <div class="c-sub" id="cnSub"></div>
      </div>
      <div class="card pain" id="cardPain" hidden>
        <div class="c-label">Pain check</div>
        <div class="c-main" id="cpMain">—</div>
        <div class="c-sub" id="cpSub"></div>
      </div>
    </div>
    <div id="log"></div>
    <form id="f">
      <div class="scope-bar" id="scopeBar" hidden></div>
      <div class="composer">
        <button type="button" id="plus" aria-label="Compose">+</button>
        <input id="t" placeholder="Ask me anything…" autocomplete="off">
        <button id="send" type="submit" aria-label="Send">➤</button>
      </div>
    </form>
  </div>
  <div class="plan-col" id="planCol">
    <div class="peek" id="peek">
      <span class="peek-handle"></span>
      <span class="peek-text" id="peekText">Loading…</span>
      <span class="peek-chev">︿</span>
    </div>
    <div class="plan-head">
      <div class="plan-title">Plan</div>
      <div class="plan-status" id="planStatus">Loading…</div>
    </div>
    <div class="plan-rows" id="planRows"></div>
    <div class="plan-foot">
      <button id="push" disabled>Push to Garmin</button>
    </div>
  </div>
</div>
<script>
// No key in the page: the session cookie authenticates every request. fetch()
// sends same-origin cookies by default, so there is nothing to pass around.
const log = document.getElementById("log"), t = document.getElementById("t");
const planCol = document.getElementById("planCol"), peek = document.getElementById("peek");
const peekText = document.getElementById("peekText");
const planRows = document.getElementById("planRows"), planStatus = document.getElementById("planStatus");
const pushBtn = document.getElementById("push");
const scopeBar = document.getElementById("scopeBar");
const KIND = { strength:"STR", conditioning:"COND", mobility:"PT", rest:"REST" };
const KIND_FULL = { strength:"Strength", conditioning:"Conditioning", mobility:"PT / mobility", rest:"Rest" };
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const BADGE = { pushed: ["on", "on watch"], modified: ["mod", "re-push"] };
const rowSig = new Map();
const openDays = new Set();
let curReadiness = null, curPain = null, serverToday = null;
let curPushStatus = {}, curDraft = [], scopeDate = null;

function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
// The model is asked for plain text but reaches for markdown anyway (headers,
// **bold**, bullet lists) when a reply is structured, e.g. a 7-day schedule.
// Render the common cases instead of fighting it turn after turn. Escaped
// first, so this only ever adds <strong>/bullet-glyph — never raw HTML from
// the model. Deliberately NOT real <ul>/<ol>: the model already numbers days
// itself, and rebuilding that as semantic lists would restart numbering at
// each indented sub-bullet break.
function renderMD(s) {
  let out = esc(String(s));
  out = out.replace(/^(#{1,4})[ \\t]+(.+)$/gm, '<strong class="md-h">$2</strong>');
  out = out.replace(/\\*\\*([^*\\n]+)\\*\\*/g, "<strong>$1</strong>");
  out = out.replace(/^([ \\t]*)[-*][ \\t]+/gm, "$1• ");
  return out;
}
// Base-workout names carry an em-dash that the model sometimes corrupts to a
// control char (e.g. "PT Day <DEL> Home"); restore it so titles read cleanly.
function cleanTitle(s) { return String(s).replace(/[\\x00-\\x1F\\x7F]+/g, " — ").replace(/\\s+/g, " ").trim(); }
function isoLocal(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
function fmtWhen(iso) {
  const d = new Date(iso + "T00:00");
  return isNaN(d) ? iso : `${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}`;
}
function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

/* --- stat cards --- */
function renderCards(draft) {
  const cr = document.getElementById("cardReady");
  cr.className = "card ready";
  if (curReadiness && curReadiness.headline) {
    cr.hidden = false;
    cr.classList.add(curReadiness.status || "steady");
    document.getElementById("crMain").textContent = curReadiness.headline;
    document.getElementById("crSub").innerHTML = (curReadiness.acwr != null)
      ? `load ratio <span class="num">${curReadiness.acwr}×</span>` : "";
    cr.title = curReadiness.detail || "";
  } else { cr.hidden = true; }

  const next = buildWeek(draft || []).find(d => d.entry && d.entry.kind !== "rest");
  if (next) {
    document.getElementById("cnMain").textContent = cleanTitle(next.entry.title);
    document.getElementById("cnSub").innerHTML =
      `${next.label} · ${KIND_FULL[next.entry.kind] || ""} · <span class="num">${Math.round(next.entry.est_duration_min || 0)}m</span>`;
  } else {
    document.getElementById("cnMain").textContent = "Nothing planned";
    document.getElementById("cnSub").textContent = "ask me to plan your week";
  }

  const cp = document.getElementById("cardPain");
  if (curPain && (curPain.level != null || curPain.location || curPain.notes)) {
    cp.hidden = false;
    document.getElementById("cpMain").innerHTML = (curPain.level != null)
      ? `<span class="num">${curPain.level}/10</span>` : esc(curPain.location || curPain.notes);
    const sub = [];
    if (curPain.level != null && curPain.location) sub.push(curPain.location);
    if (curPain.notes && curPain.notes !== (curPain.level != null ? "" : curPain.location)) sub.push(curPain.notes);
    document.getElementById("cpSub").textContent = sub.join(" · ");
  } else { cp.hidden = true; }
}

/* --- hero (empty-chat state) --- */
function showHero() {
  const hero = document.createElement("div");
  hero.className = "hero"; hero.id = "hero";
  hero.innerHTML =
    `<div class="hero-hi">${greeting()} 👋</div>` +
    `<div class="hero-line">Hey you — I'm Jim. Let's get to work. 💪</div>` +
    `<div class="hero-sub">I read your Garmin data and plan around your joints, no ego lifting on my watch. Nothing hits your watch until you push it.</div>` +
    `<div class="hero-ask">Try asking</div>`;
  const chips = document.createElement("div"); chips.className = "chips";
  [["#B4CE9E","Plan my week","plan my week"],
   ["#D98C7A","Knee's sore","my knee is sore today"],
   ["#E7B57C","Set a goal","my long-term goal is "],
   ["#9A9C90","Tomorrow?","what should I train tomorrow?"]]
    .forEach(([hue, label, msg]) => {
      const c = document.createElement("button"); c.className = "chip"; c.type = "button";
      c.innerHTML = `<i style="background:${hue}"></i>` + esc(label);
      c.onclick = () => { if (msg.endsWith(" ")) { t.value = msg; t.focus(); } else send(msg); };
      chips.appendChild(c);
    });
  hero.appendChild(chips);
  log.appendChild(hero);
}
function removeHero() { document.getElementById("hero")?.remove(); }

/* --- chat bubbles --- */
function bubble(role, node) {
  const row = document.createElement("div"); row.className = "row " + role;
  if (role === "bot") { const av = document.createElement("div"); av.className = "avatar"; row.appendChild(av); }
  const m = document.createElement("div"); m.className = "msg";
  if (typeof node !== "string") m.appendChild(node);
  else if (role === "bot") m.innerHTML = renderMD(node);
  else m.textContent = node;
  row.appendChild(m); log.appendChild(row); log.scrollTop = log.scrollHeight; return m;
}
function add(role, text) { return bubble(role, text); }
function typing() {
  const m = bubble("bot", ""); m.classList.add("busy");
  m.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  return m;
}
function settle(m, text) { m.classList.remove("busy"); m.innerHTML = renderMD(text); }

// Short holds read naturally in seconds (a 30s plank); anything a minute or
// longer reads better in minutes (1800s -> 30m).
function fmtDur(secs) {
  if (!secs) return "0s";
  if (secs < 60) return `${secs}s`;
  const mins = secs / 60;
  return `${Number.isInteger(mins) ? mins : Math.round(mins)}m`;
}
function stepLine(x) {
  const dose = x.reps ? `<span class="num">${x.sets}×${x.reps}</span>`
                      : `<span class="num">${x.sets}×${fmtDur(x.duration_sec)}</span>`;
  const wt = x.weight_kg ? ` @ <span class="num">${x.weight_kg}kg</span>` : "";
  return esc(x.exercise) + " " + dose + wt;
}
function buildWeek(draft) {
  // Anchor on the server's date (APP_TIMEZONE) so the week can't drift from the
  // browser's local day and hide a session dated "today". Falls back to local.
  const today = serverToday ? new Date(serverToday + "T00:00:00") : new Date();
  const days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
    const iso = isoLocal(d);
    days.push({ iso, label: fmtWhen(iso), entry: (draft || []).find(x => x.for_date === iso) || null, isToday: i === 0 });
  }
  return days;
}
function nowHM() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }

// A rest day (or an empty one) is still editable — you can turn it into a
// workout; it just gets an "Add a workout" action instead of a push.
function detailHtml(entry, iso, status) {
  let html = "";
  const isRest = !entry || entry.kind === "rest";
  if (entry && entry.steps && entry.steps.length) {
    html += entry.steps.map(x => {
      const note = x.notes ? `<div class="d-note">${esc(x.notes)}</div>` : "";
      return `<div class="d-step">${stepLine(x)}${note}</div>`;
    }).join("");
  }
  if (entry && entry.rationale_summary) html += `<div class="d-why">${esc(entry.rationale_summary)}</div>`;
  const acts = [`<button type="button" class="d-btn edit" data-iso="${iso}">` +
                `${isRest ? "Add a workout" : "Edit this day"}</button>`];
  if (!isRest) {
    const pushLabel = status === "pushed" ? "On watch ✓"
                    : status === "modified" ? "Update on watch" : "Push to Garmin";
    acts.push(`<button type="button" class="d-btn push" data-iso="${iso}"` +
              `${status === "pushed" ? " disabled" : ""}>${pushLabel}</button>`);
  }
  html += `<div class="d-actions">${acts.join("")}</div>`;
  return html;
}

function renderPlan(draft, opts) {
  opts = opts || {};
  curDraft = draft || [];
  const pulseOn = opts.pulse !== false;
  const days = buildWeek(draft);
  planRows.innerHTML = "";
  let nextPeek = null, focusRow = null, todayRow = null;
  for (const day of days) {
    const entry = day.entry, isRest = !entry || entry.kind === "rest";
    const sig = entry ? JSON.stringify([entry.kind, entry.title, entry.est_duration_min, entry.steps]) : "REST";
    const prevSig = rowSig.get(day.iso);
    const changed = pulseOn && prevSig !== undefined && prevSig !== sig;
    rowSig.set(day.iso, sig);

    const status = curPushStatus[day.iso] || null;   // "pushed" | "modified" | null
    const badge = BADGE[status]
      ? `<span class="r-badge ${BADGE[status][0]}">${BADGE[status][1]}</span>` : "";
    const typeLabel = isRest ? "—" : (KIND[entry.kind] || "—");
    const title = isRest ? "Rest" : cleanTitle(entry.title);
    const dur = (!isRest && entry.est_duration_min) ? `${Math.round(entry.est_duration_min)}m` : "";
    const steps = (!isRest && entry.steps && entry.steps.length) ? entry.steps.map(stepLine).join(" &middot; ") : "";
    const detail = detailHtml(entry, day.iso, status);  // rest/empty days stay editable
    const clickable = true;
    if (changed && clickable) openDays.add(day.iso);  // auto-open a day that just changed

    const row = document.createElement("div");
    row.className = "row-day" + (day.isToday ? " today" : "") + (isRest ? " rest" : "")
      + (changed ? " pulse" : "") + (clickable ? " clickable" : "") + (openDays.has(day.iso) ? " open" : "");
    row.innerHTML =
      `<div class="row-top"><span class="r-type">${typeLabel}</span>` +
      `<span class="r-title">${esc(title)}</span><span class="r-dur">${dur}</span>` +
      `<button type="button" class="r-edit" data-iso="${day.iso}" title="${isRest ? "Add a workout" : "Edit this day"}">✎</button>` +
      (clickable ? `<span class="r-chev">&rsaquo;</span>` : "") + `</div>` +
      `<div class="row-sub"><span class="r-date">${day.label}</span>` + badge +
      (steps ? `<span class="r-steps">${steps}</span>` : "") + `</div>` +
      (detail ? `<div class="row-detail">${detail}</div>` : "");
    if (clickable) row.addEventListener("click", () => {
      const nowOpen = row.classList.toggle("open");
      if (nowOpen) openDays.add(day.iso); else openDays.delete(day.iso);
    });
    row.querySelector(".r-edit")?.addEventListener("click", (e) => {
      e.stopPropagation(); setScope(day.iso, day.label);
    });
    row.querySelector(".d-btn.edit")?.addEventListener("click", (e) => {
      e.stopPropagation(); setScope(day.iso, day.label);
    });
    row.querySelector(".d-btn.push")?.addEventListener("click", (e) => {
      e.stopPropagation(); pushDay(day.iso, e.currentTarget);
    });
    if (changed) row.addEventListener("animationend", () => row.classList.remove("pulse"), { once: true });
    planRows.appendChild(row);
    if (day.isToday) todayRow = row;
    if (changed && !focusRow) focusRow = row;
    if (!isRest && !nextPeek) nextPeek = `${day.label} &middot; ${esc(title)} &middot; <span class="num">${dur}</span>`;
  }
  peekText.innerHTML = nextPeek || "Nothing planned this week";
  const hasPlan = (draft || []).length > 0;
  pushBtn.disabled = !hasPlan;
  pushBtn.classList.remove("syncing");
  pushBtn.textContent = "Push to Garmin";
  planStatus.textContent = hasPlan ? "Draft · not pushed yet" : "Nothing planned yet";
  renderCards(draft);

  if (opts.focus) focusPlan(focusRow || todayRow);
}

/* Bring the panel and the relevant day into view when the plan changes. */
function focusPlan(row) {
  if (window.matchMedia("(max-width: 880px)").matches) planCol.classList.add("expanded");
  if (row) requestAnimationFrame(() => row.scrollIntoView({ behavior: "smooth", block: "nearest" }));
}
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ ...body }) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "error");
  return data;
}
/* --- edit scope (day vs. week) --- */
function renderScopeBar() {
  if (!scopeDate) { scopeBar.hidden = true; scopeBar.innerHTML = ""; t.placeholder = "Ask me anything…"; return; }
  scopeBar.hidden = false;
  scopeBar.innerHTML = `<span class="scope-pill">Editing ${esc(fmtWhen(scopeDate))}` +
    `<button type="button" id="scopeX" aria-label="Clear">✕</button></span>`;
  document.getElementById("scopeX").onclick = clearScope;
  t.placeholder = "What should change on this day?";
}
function setScope(iso) { scopeDate = iso; renderScopeBar(); t.focus(); }
function clearScope() { scopeDate = null; renderScopeBar(); }

async function pushDay(iso, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
  try {
    const data = await api("/chat/push-day", { date: iso });
    add("bot", data.summary);
    if (data.push_status) curPushStatus = data.push_status;
    renderPlan(data.draft || curDraft, { pulse: false });
  } catch (err) { add("bot", err.message); if (btn) { btn.disabled = false; btn.textContent = "Push to Garmin"; } }
}

async function send(text) {
  removeHero();
  add("me", text); t.value = "";
  const busy = typing();
  const scoped = scopeDate;
  try {
    const data = await api("/chat/message", scoped ? { text, scope_date: scoped } : { text });
    settle(busy, data.reply);
    if (data.today) serverToday = data.today;
    if (data.push_status) curPushStatus = data.push_status;
    if (data.draft !== null && data.draft !== undefined) renderPlan(data.draft, { focus: true });
  } catch (err) { settle(busy, err.message); }
}
async function load() {
  rowSig.clear();
  try {
    const r = await fetch("/chat/state");
    const s = await r.json();
    if (!r.ok) { add("bot", s.detail || "error"); return; }
    curReadiness = s.readiness || null;
    curPain = s.pain || null;
    curPushStatus = s.push_status || {};
    serverToday = s.today || serverToday;
    if (!s.history.length) showHero();
    for (const m of s.history) add(m.role === "user" ? "me" : "bot", m.content);
    renderPlan(s.draft, { pulse: false });
  } catch { add("bot", "network error — reload"); }
}
document.getElementById("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = t.value.trim(); if (text) send(text);
});
document.getElementById("plus").addEventListener("click", () => t.focus());
pushBtn.addEventListener("click", async () => {
  pushBtn.disabled = true; pushBtn.classList.add("syncing"); pushBtn.textContent = "Syncing…";
  try {
    const data = await api("/chat/approve", {});
    add("bot", data.summary);
    if (data.push_status) curPushStatus = data.push_status;
    if (data.draft) renderPlan(data.draft, { pulse: false });
    pushBtn.classList.remove("syncing"); pushBtn.textContent = "Push to Garmin";
    planStatus.textContent = "On watch · synced " + nowHM();
  } catch (err) {
    add("bot", err.message);
    pushBtn.disabled = false; pushBtn.classList.remove("syncing"); pushBtn.textContent = "Push to Garmin";
  }
});
document.getElementById("clear").addEventListener("click", async (e) => {
  e.preventDefault();
  try { await api("/chat/clear", {}); log.innerHTML = ""; load(); } catch {}
});
function setExpanded(v) { planCol.classList.toggle("expanded", v); }
let dragStartY = null, dragMoved = false;
peek.addEventListener("touchstart", e => { dragStartY = e.touches[0].clientY; dragMoved = false; }, { passive: true });
peek.addEventListener("touchmove", e => {
  if (dragStartY === null) return;
  const dy = e.touches[0].clientY - dragStartY;
  if (Math.abs(dy) > 10) dragMoved = true;
  if (dy < -30) setExpanded(true); else if (dy > 30) setExpanded(false);
}, { passive: true });
peek.addEventListener("touchend", () => { dragStartY = null; });
peek.addEventListener("click", () => { if (!dragMoved) setExpanded(!planCol.classList.contains("expanded")); dragMoved = false; });
load();
</script></body></html>"""


@app.get("/chat")
def chat_page(request: Request, key: str = "") -> Response:
    """Signing in with ?key= sets the session cookie and bounces to a clean /chat,
    so the secret is never left sitting in the address bar or browser history.
    After that the cookie carries it and the URL is just /chat."""
    _require_auth(request, key)
    if key:
        redirect = RedirectResponse("/chat", status_code=303)
        _set_session(redirect, secure=request.url.scheme == "https")
        return redirect
    return HTMLResponse(CHAT_PAGE)
