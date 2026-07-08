"""Thin FastAPI service (PLAN.md §5): health check, manual trigger, and the
built-in web chat (a private, mobile-friendly page — add it to your home
screen and it behaves like a messaging app). The agent is a callable, not tied
to HTTP — Render Cron invokes the nightly job directly."""

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from jim.agent.loop import run_agent
from jim.chat import handle_chat_message
from jim.config import settings

app = FastAPI(title="jim")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def trigger_run() -> dict:
    """Manual nightly run (same as the cron job, minus the data sync)."""
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


# --- built-in web chat ------------------------------------------------------


def _check_key(key: str) -> None:
    secret = settings().chat_secret
    if not secret or not hmac.compare_digest(key, secret):
        raise HTTPException(status_code=403, detail="bad or missing chat key")


class ChatMessage(BaseModel):
    key: str
    text: str


@app.post("/chat/message")
def chat_message(msg: ChatMessage) -> dict[str, str]:
    _check_key(msg.key)
    if not msg.text.strip():
        raise HTTPException(status_code=400, detail="empty message")
    now = datetime.now(ZoneInfo(settings().app_timezone))
    return {"reply": handle_chat_message(msg.text, now)}


CHAT_PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Jim</title>
<style>
* { box-sizing: border-box; margin: 0; }
body { font-family: -apple-system, system-ui, sans-serif; background: #f8f7f4; height: 100dvh;
       display: flex; flex-direction: column; }
header { background: #1c2b3a; color: #fff; padding: 14px 16px; font-weight: 700; }
#log { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.msg { max-width: 85%; padding: 10px 12px; border-radius: 14px; font-size: 14px;
       white-space: pre-wrap; line-height: 1.45; }
.me { align-self: flex-end; background: #1c2b3a; color: #fff; border-bottom-right-radius: 4px; }
.bot { align-self: flex-start; background: #fff; border: 1px solid #e3e8ee;
       border-bottom-left-radius: 4px; }
.bot.busy { color: #7a90a4; font-style: italic; }
form { display: flex; gap: 8px; padding: 10px 12px calc(10px + env(safe-area-inset-bottom));
       background: #fff; border-top: 1px solid #e3e8ee; }
input { flex: 1; padding: 11px 14px; border: 1px solid #cbd5df; border-radius: 22px;
        font-size: 15px; outline: none; }
button { padding: 0 18px; border: none; border-radius: 22px; background: #1c2b3a;
         color: #fff; font-weight: 600; font-size: 14px; }
</style></head><body>
<header>Jim — tell me about your day</header>
<div id="log"><div class="msg bot">Tell me how you're feeling, where you'll be, or what you \
want — I'll plan around it. Example: "left knee sore, home only, 30 min".</div></div>
<form id="f"><input id="t" placeholder="Message" autocomplete="off"><button>Send</button></form>
<script>
const key = new URLSearchParams(location.search).get("key") || "";
const log = document.getElementById("log"), t = document.getElementById("t");
function add(cls, text) {
  const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = t.value.trim(); if (!text) return;
  add("me", text); t.value = "";
  const busy = add("bot busy", "planning…");
  try {
    const r = await fetch("/chat/message", { method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ key, text }) });
    const data = await r.json();
    busy.textContent = r.ok ? data.reply : (data.detail || "error");
    busy.classList.remove("busy");
  } catch { busy.textContent = "network error — try again"; busy.classList.remove("busy"); }
});
</script></body></html>"""


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> str:
    _check_key(key)
    return CHAT_PAGE
