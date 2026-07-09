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
<meta name="theme-color" content="#15161B">
<title>Jim</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;800&family=Inter:wght@400;450;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#15161B; --panel:#1E2027; --line:#2A2D35; --ink:#EDEAE3; --muted:#8B9099;
  --accent:#C1602A; --data:#F0A63C; --user:#262A33; --well:#181920;
}
* { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg);
       color: var(--ink); height: 100dvh; display: flex; flex-direction: column;
       -webkit-font-smoothing: antialiased; }
header { padding: 18px 18px 14px; display: flex; align-items: baseline; gap: 11px;
         border-bottom: 1px solid var(--line); background: var(--bg); z-index: 5; }
.hname { font-family: 'Barlow Condensed', sans-serif; font-weight: 800; font-size: 23px;
         letter-spacing: .05em; text-transform: uppercase; }
.hname i { color: var(--accent); font-style: normal; }
.htag { font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: .14em;
        text-transform: uppercase; color: var(--muted); flex: 1; }
#clear { font-family: 'JetBrains Mono', monospace; color: var(--muted); font-size: 10.5px;
         letter-spacing: .08em; text-transform: uppercase; text-decoration: none; }
#clear:hover { color: var(--ink); }
#log { flex: 1; overflow-y: auto; padding: 18px 14px 8px; display: flex;
       flex-direction: column; gap: 10px; }
.row { display: flex; max-width: 84%; }
.row.me { align-self: flex-end; }
.row.bot { align-self: flex-start; }
.msg { padding: 11px 14px; border-radius: 3px; font-size: 14.5px; line-height: 1.55;
       white-space: pre-wrap; word-wrap: break-word; }
.me .msg { background: var(--user); color: var(--ink); }
.bot .msg { background: var(--panel); color: var(--ink); border-left: 2px solid var(--accent); }
.msg.busy { display: flex; gap: 4px; align-items: center; padding: 14px; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted);
       animation: bounce 1.3s infinite; }
.dot:nth-child(2){ animation-delay:.16s } .dot:nth-child(3){ animation-delay:.32s }
@keyframes bounce { 0%,64%,100%{ transform: translateY(0); opacity:.4 }
                    32%{ transform: translateY(-5px); opacity:1 } }
.chips { display: flex; flex-wrap: wrap; gap: 7px; padding: 2px; align-self: flex-start; }
.chip { border: 1px solid var(--line); background: transparent; color: var(--muted);
        border-radius: 3px; padding: 8px 12px; font-size: 12.5px; font-weight: 500;
        font-family: 'Inter'; cursor: pointer; }
.chip:hover { border-color: var(--accent); color: var(--ink); }
.chip:active { transform: scale(.97); }
#draft { display: none; margin: 8px 14px 2px; background: var(--panel); border-radius: 3px;
         padding: 16px 17px; border: 1px solid var(--line); }
.draft-h { font-family: 'Barlow Condensed'; font-weight: 800; font-size: 14px;
           letter-spacing: .08em; text-transform: uppercase; display: flex; align-items: center; gap: 8px; }
.draft-h::before { content:""; width: 7px; height: 7px; background: var(--accent); }
.draft-sub { font-family: 'JetBrains Mono'; font-size: 10px; letter-spacing: .04em;
             text-transform: uppercase; color: var(--muted); margin: 5px 0 12px 15px; }
.day { display: flex; gap: 11px; padding: 12px 0 0; align-items: flex-start; position: relative; }
.day:first-of-type { padding-top: 0; }
.day:not(:first-of-type)::before { content:""; position: absolute; top: 0; left: 0; right: 0;
    height: 1px; background-image: radial-gradient(circle, var(--line) 1px, transparent 1.4px);
    background-size: 7px 1px; background-repeat: repeat-x; }
.day-kind { font-family: 'JetBrains Mono'; font-size: 9.5px; letter-spacing: .06em;
            text-transform: uppercase; color: var(--accent); border: 1px solid var(--accent);
            border-radius: 2px; padding: 3px 5px; flex-shrink: 0; margin-top: 2px; }
.day-main { flex: 1; min-width: 0; }
.day-top { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
.day-title { font-weight: 600; font-size: 14.5px; padding-top: 2px; }
.day-when { font-family: 'JetBrains Mono'; font-size: 10.5px; color: var(--muted);
            white-space: nowrap; padding-top: 4px; text-transform: uppercase; letter-spacing: .04em; }
.readout { display: inline-flex; flex-direction: column; align-items: center; justify-content: center;
           background: var(--well); border: 1px solid var(--line); border-radius: 3px;
           padding: 4px 9px; min-width: 42px; flex-shrink: 0; }
.readout .num { font-family: 'JetBrains Mono'; font-weight: 600; font-size: 15px;
                color: var(--data); line-height: 1.1; font-variant-numeric: tabular-nums; }
.readout .lbl { font-family: 'JetBrains Mono'; font-size: 8px; letter-spacing: .1em;
                color: var(--muted); margin-top: 2px; text-transform: uppercase; }
.day-steps { font-size: 12.5px; color: var(--muted); margin-top: 5px; line-height: 1.6; }
.day-steps .num { font-family: 'JetBrains Mono'; color: var(--data); font-weight: 500; }
#push { margin-top: 16px; width: 100%; padding: 13px; border: none; border-radius: 3px;
        background: var(--accent); color: var(--ink); font-weight: 600; font-size: 13px;
        font-family: 'JetBrains Mono'; letter-spacing: .08em; text-transform: uppercase; cursor: pointer; }
#push:active { transform: translateY(1px); }
form { display: flex; gap: 9px; padding: 12px 14px calc(12px + env(safe-area-inset-bottom));
       background: var(--bg); border-top: 1px solid var(--line); }
#t { flex: 1; padding: 12px 16px; border: 1px solid var(--line); border-radius: 3px;
     font-size: 15px; font-family: 'Inter'; font-weight: 450; outline: none;
     background: var(--well); color: var(--ink); }
#t::placeholder { color: var(--muted); }
#t:focus { border-color: var(--accent); }
#send { border: 1px solid var(--line); border-radius: 3px; width: 46px; height: 46px;
        background: var(--well); color: var(--ink); font-size: 17px; cursor: pointer; flex-shrink: 0; }
#send:active { transform: scale(.95); }
</style></head><body>
<header>
  <div class="hname">Jim<i>.</i></div>
  <div class="htag">training coach</div>
  <a href="#" id="clear">Clear</a>
</header>
<div id="log"></div>
<div id="draft">
  <div class="draft-h">Working plan</div>
  <div class="draft-sub">Not on your watch yet — review, then push.</div>
  <div id="draft-body"></div>
  <button id="push">Push to Garmin</button>
</div>
<form id="f">
  <input id="t" placeholder="Message Jim…" autocomplete="off">
  <button id="send" type="submit" aria-label="Send">↑</button>
</form>
<script>
const key = new URLSearchParams(location.search).get("key") || "";
const log = document.getElementById("log"), t = document.getElementById("t");
const draftBox = document.getElementById("draft");
const draftBody = document.getElementById("draft-body");
const KIND = { strength:"STR", conditioning:"COND", mobility:"PT", rest:"REST" };
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

function bubble(role, node) {
  const row = document.createElement("div"); row.className = "row " + role;
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

function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function stepLine(x) {
  const dose = x.reps ? `<span class="num">${x.sets}×${x.reps}</span>` : `<span class="num">${x.sets}×${x.duration_sec}s</span>`;
  const wt = x.weight_kg ? ` @ <span class="num">${x.weight_kg}kg</span>` : "";
  return esc(x.exercise) + " " + dose + wt;
}
function renderDraft(draft) {
  draftBody.innerHTML = "";
  if (!draft || !draft.length) { draftBox.style.display = "none"; return; }
  for (const s of draft) {
    const row = document.createElement("div"); row.className = "day";
    const kind = document.createElement("div"); kind.className = "day-kind";
    kind.textContent = KIND[s.kind] || "—";
    const main = document.createElement("div"); main.className = "day-main";
    const d = new Date(s.for_date + "T00:00");
    const when = isNaN(d) ? s.for_date : `${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}`;
    const steps = (s.steps || []).map(stepLine).join(" &middot; ");
    main.innerHTML =
      `<div class="day-top"><span class="day-title">${esc(s.title)}</span>` +
      `<div class="readout"><span class="num">${Math.round(s.est_duration_min)}</span><span class="lbl">min</span></div></div>` +
      `<div class="day-when">${when}</div>` +
      (steps ? `<div class="day-steps">${steps}</div>` : "");
    row.appendChild(kind); row.appendChild(main); draftBody.appendChild(row);
  }
  draftBox.style.display = "block";
}
function showChips() {
  const wrap = document.createElement("div"); wrap.className = "chips";
  [["Plan my week","plan my week"], ["Knee's sore","my knee is sore today"],
   ["Set a goal","my long-term goal is "], ["Tomorrow?","what should I train tomorrow?"]]
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
  } catch (err) { settle(busy, err.message); }
}
async function load() {
  try {
    const r = await fetch(`/chat/state?key=${encodeURIComponent(key)}`);
    const s = await r.json();
    if (!r.ok) { add("bot", s.detail || "error"); return; }
    if (!s.history.length) {
      add("bot", "Hey — I'm Jim. Tell me how you're feeling, what you want this week, " +
        "or a long-term goal. I'll draft it here and nothing hits your watch until you push.");
      showChips();
    }
    for (const m of s.history) add(m.role === "user" ? "me" : "bot", m.content);
    renderDraft(s.draft);
  } catch { add("bot", "network error — reload"); }
}
document.getElementById("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = t.value.trim(); if (text) send(text);
});
document.getElementById("push").addEventListener("click", async () => {
  const busy = typing();
  try {
    const data = await api("/chat/approve", {});
    settle(busy, data.summary); renderDraft([]);
  } catch (err) { settle(busy, err.message); }
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
