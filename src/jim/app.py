"""Thin FastAPI service (PLAN.md §5): health check, manual nightly trigger, and
Jim's chat — a private, mobile-friendly page (add to home screen) where the
athlete iterates on plans and pushes them to Garmin on approve. The agent is a
callable, not tied to HTTP — Render Cron invokes the nightly job directly."""

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from jim import coach
from jim.agent.loop import run_agent
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


# --- Jim's chat ---------------------------------------------------------------


def _check_key(key: str) -> None:
    secret = settings().chat_secret
    if not secret or not hmac.compare_digest(key, secret):
        raise HTTPException(status_code=403, detail="bad or missing chat key")


class KeyOnly(BaseModel):
    key: str


class ChatMessage(BaseModel):
    key: str
    text: str


@app.post("/chat/message")
def chat_message(msg: ChatMessage) -> dict:
    _check_key(msg.key)
    if not msg.text.strip():
        raise HTTPException(status_code=400, detail="empty message")
    return coach.converse(msg.text.strip())


@app.post("/chat/approve")
def chat_approve(body: KeyOnly) -> dict:
    _check_key(body.key)
    return {"summary": coach.approve()}


@app.post("/chat/clear")
def chat_clear(body: KeyOnly) -> dict:
    _check_key(body.key)
    coach.clear()
    return {"ok": True}


@app.get("/chat/state")
def chat_state(key: str = "") -> dict:
    _check_key(key)
    return coach.current_state()


CHAT_PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Jim · your training buddy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #fff6ee; --panel: #ffffff; --ink: #2a2440; --muted: #8b8398;
  --line: #f0e6dc; --me1: #ff7a4d; --me2: #ff5c8a; --accent: #12b39a;
  --accent2: #2bd4a8; --shadow: 0 6px 18px rgba(120,80,60,.10);
  --strength: #ff7a4d; --mobility: #8a5cff; --conditioning: #1ea7d6;
  --rest: #9aa2b1; --goal: #f6b73c;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #17151f; --panel: #221f2e; --ink: #f2eef8; --muted: #9d94ad;
    --line: #322d40; --shadow: 0 6px 18px rgba(0,0,0,.35); }
}
:root[data-theme="light"] { --bg:#fff6ee; --panel:#fff; --ink:#2a2440; --muted:#8b8398; --line:#f0e6dc; }
:root[data-theme="dark"] { --bg:#17151f; --panel:#221f2e; --ink:#f2eef8; --muted:#9d94ad; --line:#322d40; }
* { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: 'Nunito', -apple-system, system-ui, sans-serif; background: var(--bg);
       color: var(--ink); height: 100dvh; display: flex; flex-direction: column; }
header { background: linear-gradient(120deg, #ff7a4d, #ff5c8a 90%); color: #fff;
         padding: 14px 16px; display: flex; align-items: center; gap: 11px;
         box-shadow: 0 2px 14px rgba(255,110,90,.35); z-index: 5; }
.avatar { width: 40px; height: 40px; border-radius: 50%; background: rgba(255,255,255,.22);
          display: grid; place-items: center; font-size: 22px; flex-shrink: 0;
          box-shadow: inset 0 0 0 2px rgba(255,255,255,.35); }
.hgrow { flex: 1; min-width: 0; }
.hname { font-family: 'Fredoka', sans-serif; font-weight: 700; font-size: 20px;
         letter-spacing: .3px; line-height: 1; }
.htag { font-size: 11.5px; opacity: .9; font-weight: 600; margin-top: 2px; }
#clear { color: #fff; opacity: .85; font-size: 12px; font-weight: 700; text-decoration: none;
         background: rgba(255,255,255,.18); padding: 6px 10px; border-radius: 20px; }
#log { flex: 1; overflow-y: auto; padding: 16px 12px 6px; display: flex;
       flex-direction: column; gap: 10px; }
.row { display: flex; gap: 8px; align-items: flex-end; max-width: 90%; }
.row.me { align-self: flex-end; flex-direction: row-reverse; }
.row.bot { align-self: flex-start; }
.pic { width: 28px; height: 28px; border-radius: 50%; display: grid; place-items: center;
       font-size: 16px; flex-shrink: 0; background: linear-gradient(135deg,#ff7a4d,#ff5c8a); }
.msg { padding: 11px 14px; border-radius: 20px; font-size: 14.5px; line-height: 1.5;
       white-space: pre-wrap; word-wrap: break-word; box-shadow: var(--shadow); }
.me .msg { background: linear-gradient(135deg, var(--me1), var(--me2)); color: #fff;
           border-bottom-right-radius: 6px; }
.bot .msg { background: var(--panel); color: var(--ink); border-bottom-left-radius: 6px; }
.msg.busy { display: flex; gap: 5px; align-items: center; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--me1);
       animation: bounce 1.2s infinite; }
.dot:nth-child(2){ animation-delay:.15s } .dot:nth-child(3){ animation-delay:.3s }
@keyframes bounce { 0%,60%,100%{ transform: translateY(0); opacity:.5 }
                    30%{ transform: translateY(-6px); opacity:1 } }
.chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 4px 2px; align-self: flex-start; }
.chip { border: 1.5px solid var(--line); background: var(--panel); color: var(--ink);
        border-radius: 20px; padding: 8px 13px; font-size: 13px; font-weight: 700;
        font-family: 'Nunito'; cursor: pointer; box-shadow: var(--shadow); }
.chip:active { transform: scale(.96); }
#draft { display: none; margin: 6px 12px 2px; background: var(--panel); border-radius: 20px;
         padding: 14px; box-shadow: var(--shadow); border: 2px solid var(--accent); }
.draft-h { font-family: 'Fredoka'; font-weight: 700; font-size: 13px; color: var(--accent);
           display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.draft-sub { font-size: 11.5px; color: var(--muted); font-weight: 600; margin-bottom: 10px; }
.day { display: flex; gap: 10px; padding: 9px 0; border-top: 1px solid var(--line); }
.day:first-of-type { border-top: none; }
.day-ic { width: 34px; height: 34px; border-radius: 11px; display: grid; place-items: center;
          font-size: 18px; flex-shrink: 0; }
.day-main { flex: 1; min-width: 0; }
.day-top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.day-title { font-weight: 800; font-size: 14px; }
.day-when { font-size: 11px; color: var(--muted); font-weight: 700; white-space: nowrap; }
.pill { font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .4px;
        padding: 2px 8px; border-radius: 20px; color: #fff; display: inline-block; margin-top: 3px; }
.day-steps { font-size: 12.5px; color: var(--muted); margin-top: 4px; line-height: 1.5; }
#push { margin-top: 12px; width: 100%; padding: 13px; border: none; border-radius: 15px;
        background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff;
        font-weight: 800; font-size: 14.5px; font-family: 'Nunito'; cursor: pointer;
        box-shadow: 0 5px 14px rgba(18,179,154,.4); }
#push:active { transform: translateY(1px); }
form { display: flex; gap: 9px; padding: 11px 12px calc(11px + env(safe-area-inset-bottom));
       background: var(--panel); border-top: 1px solid var(--line); }
#t { flex: 1; padding: 12px 16px; border: 1.5px solid var(--line); border-radius: 24px;
     font-size: 15px; font-family: 'Nunito'; font-weight: 600; outline: none;
     background: var(--bg); color: var(--ink); }
#t:focus { border-color: var(--me1); }
#send { padding: 0 16px; border: none; border-radius: 50%; width: 48px; height: 48px;
        background: linear-gradient(135deg, var(--me1), var(--me2)); color: #fff;
        font-size: 18px; cursor: pointer; flex-shrink: 0; box-shadow: var(--shadow); }
#send:active { transform: scale(.94); }
</style></head><body>
<header>
  <div class="avatar">🏋️</div>
  <div class="hgrow"><div class="hname">Jim</div><div class="htag">your training buddy 💪</div></div>
  <a href="#" id="clear">clear</a>
</header>
<div id="log"></div>
<div id="draft">
  <div class="draft-h">📋 Working plan</div>
  <div class="draft-sub">Not on your watch yet — review, then push. ⌚</div>
  <div id="draft-body"></div>
  <button id="push">⌚ Push to Garmin</button>
</div>
<form id="f">
  <input id="t" placeholder="Message Jim…" autocomplete="off">
  <button id="send" type="submit" aria-label="Send">➤</button>
</form>
<script>
const key = new URLSearchParams(location.search).get("key") || "";
const log = document.getElementById("log"), t = document.getElementById("t");
const draftBox = document.getElementById("draft");
const draftBody = document.getElementById("draft-body");
const KIND = {
  strength:     {emoji:"🏋️", color:"var(--strength)"},
  conditioning: {emoji:"🚴", color:"var(--conditioning)"},
  mobility:     {emoji:"🧘", color:"var(--mobility)"},
  rest:         {emoji:"😴", color:"var(--rest)"},
};
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

function bubble(role, node) {
  const row = document.createElement("div"); row.className = "row " + role;
  if (role === "bot") { const p = document.createElement("div"); p.className = "pic";
    p.textContent = "🏋️"; row.appendChild(p); }
  const m = document.createElement("div"); m.className = "msg";
  if (typeof node === "string") m.textContent = node; else m.appendChild(node);
  row.appendChild(m); log.appendChild(row); log.scrollTop = log.scrollHeight; return m;
}
function add(role, text) { return bubble(role, text); }
function typing() {
  const m = bubble("bot", ""); m.classList.add("busy");
  m.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  return m;
}
function settle(m, text) { m.classList.remove("busy"); m.textContent = text; }

function renderDraft(draft) {
  draftBody.innerHTML = "";
  if (!draft || !draft.length) { draftBox.style.display = "none"; return; }
  for (const s of draft) {
    const k = KIND[s.kind] || {emoji:"✨", color:"var(--muted)"};
    const row = document.createElement("div"); row.className = "day";
    const ic = document.createElement("div"); ic.className = "day-ic";
    ic.style.background = k.color + "22"; ic.textContent = k.emoji;
    const main = document.createElement("div"); main.className = "day-main";
    const d = new Date(s.for_date + "T00:00");
    const when = isNaN(d) ? s.for_date : `${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}`;
    const steps = (s.steps || []).map(x => {
      const dose = x.reps ? `${x.sets}×${x.reps}` : `${x.sets}×${x.duration_sec}s`;
      return x.exercise + " " + dose + (x.weight_kg ? ` @ ${x.weight_kg}kg` : "");
    }).join(" · ");
    main.innerHTML =
      `<div class="day-top"><span class="day-title"></span><span class="day-when"></span></div>` +
      `<span class="pill" style="background:${k.color}">${s.kind}</span>` +
      (steps ? `<div class="day-steps"></div>` : "");
    main.querySelector(".day-title").textContent = s.title;
    main.querySelector(".day-when").textContent = `${when} · ~${Math.round(s.est_duration_min)}m`;
    if (steps) main.querySelector(".day-steps").textContent = steps;
    row.appendChild(ic); row.appendChild(main); draftBody.appendChild(row);
  }
  draftBox.style.display = "block";
}
function showChips() {
  const wrap = document.createElement("div"); wrap.className = "chips";
  [["🗓️ Plan my week","plan my week"], ["🦵 Knee's sore today","my knee is sore today"],
   ["🎯 Set a goal","my long-term goal is "], ["💪 What should I do tomorrow?","what should I train tomorrow?"]]
    .forEach(([label, msg]) => {
      const c = document.createElement("button"); c.className = "chip"; c.textContent = label;
      c.onclick = () => { if (msg.endsWith(" ")) { t.value = msg; t.focus(); } else send(msg); };
      wrap.appendChild(c);
    });
  log.appendChild(wrap);
}
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ key, ...body }) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "error");
  return data;
}
async function send(text) {
  add("me", text); t.value = "";
  document.querySelectorAll(".chips").forEach(c => c.remove());
  const busy = typing();
  try {
    const data = await api("/chat/message", { text });
    settle(busy, data.reply); renderDraft(data.draft);
  } catch (err) { settle(busy, "😕 " + err.message); }
}
async function load() {
  try {
    const r = await fetch(`/chat/state?key=${encodeURIComponent(key)}`);
    const s = await r.json();
    if (!r.ok) { add("bot", s.detail || "error"); return; }
    if (!s.history.length) {
      add("bot", "Hey! I'm Jim, your training buddy. 💪 Tell me how you're feeling, " +
        "what you want this week, or a long-term goal — I'll build the plan and nothing " +
        "hits your watch until you push it. ⌚");
      showChips();
    }
    for (const m of s.history) add(m.role === "user" ? "me" : "bot", m.content);
    renderDraft(s.draft);
  } catch { add("bot", "😕 network error — reload"); }
}
document.getElementById("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = t.value.trim(); if (text) send(text);
});
document.getElementById("push").addEventListener("click", async () => {
  const busy = typing();
  try {
    const data = await api("/chat/approve", {});
    settle(busy, "✅ " + data.summary); renderDraft([]);
  } catch (err) { settle(busy, "😕 " + err.message); }
});
document.getElementById("clear").addEventListener("click", async (e) => {
  e.preventDefault();
  try { await api("/chat/clear", {}); log.innerHTML = ""; load(); } catch {}
});
load();
</script></body></html>"""


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> str:
    _check_key(key)
    return CHAT_PAGE
