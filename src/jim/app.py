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
<link href="https://fonts.googleapis.com/css2?family=Anton&family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#15161B; --panel:#1E2027; --line:#2A2D35; --ink:#EDEAE3; --muted:#8B9099;
  --accent:#C1602A; --accent2:#E08A45; --data:#F0A63C; --user:#262A33; --well:#181920;
}
* { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: 'IBM Plex Sans', -apple-system, system-ui, sans-serif;
       background:
         radial-gradient(1100px 520px at 12% -8%, #23262f 0%, transparent 58%),
         radial-gradient(900px 480px at 108% 6%, rgba(193,96,42,.10) 0%, transparent 55%),
         linear-gradient(180deg, #17181e 0%, var(--bg) 38%);
       color: var(--ink); height: 100dvh; display: flex; flex-direction: column;
       -webkit-font-smoothing: antialiased; overflow: hidden; }
header { padding: 14px 20px; display: flex; align-items: baseline; gap: 11px;
         border-bottom: 1px solid var(--line); background: transparent; z-index: 5; flex-shrink: 0; }
.hname { font-family: 'Anton', sans-serif; font-weight: 400; font-size: 22px;
         letter-spacing: .04em; text-transform: uppercase;
         background: linear-gradient(120deg, var(--ink) 40%, #b9bcc4 100%);
         -webkit-background-clip: text; background-clip: text; color: transparent; }
.hname i { background: linear-gradient(135deg, var(--accent2), var(--accent));
           -webkit-background-clip: text; background-clip: text; color: transparent;
           font-style: normal; }
.htag { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; letter-spacing: .14em;
        text-transform: uppercase; color: var(--muted); flex: 1; }
#clear { font-family: 'JetBrains Mono', monospace; color: var(--muted); font-size: 10px;
         letter-spacing: .08em; text-transform: uppercase; text-decoration: none; }
#clear:hover { color: var(--ink); }

.main { flex: 1; display: flex; min-height: 0; position: relative; }
.chat-col { flex: 1 1 58%; min-width: 0; display: flex; flex-direction: column;
            border-right: 1px solid var(--line); }
.plan-col { flex: 0 0 42%; max-width: 400px; min-width: 300px; display: flex;
            flex-direction: column; background: var(--bg); }

/* --- chat: conversation only, monochrome ------------------------------- */
#log { flex: 1; overflow-y: auto; padding: 18px 16px 8px; display: flex;
       flex-direction: column; gap: 10px; }
.row { display: flex; max-width: 86%; gap: 8px; }
.row.me { align-self: flex-end; }
.row.bot { align-self: flex-start; align-items: flex-end; }
.avatar { width: 8px; height: 8px; border-radius: 1px;
          background: linear-gradient(135deg, var(--accent2), var(--accent));
          flex-shrink: 0; margin-bottom: 9px; }
.msg { padding: 11px 14px; border-radius: 3px; font-size: 14.5px; line-height: 1.55;
       white-space: pre-wrap; word-wrap: break-word; }
.me .msg { background: var(--user); color: var(--ink); }
.bot .msg { background: var(--panel); color: var(--ink); }
.msg.busy { display: flex; gap: 4px; align-items: center; padding: 14px; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted);
       animation: bounce 1.3s infinite; }
.dot:nth-child(2){ animation-delay:.16s } .dot:nth-child(3){ animation-delay:.32s }
@keyframes bounce { 0%,64%,100%{ transform: translateY(0); opacity:.4 }
                    32%{ transform: translateY(-5px); opacity:1 } }
.chips { display: flex; flex-wrap: wrap; gap: 7px; padding: 2px; align-self: flex-start; }
.chip { border: 1px solid var(--line); background: transparent; color: var(--muted);
        border-radius: 3px; padding: 8px 12px; font-size: 12.5px; font-weight: 500;
        font-family: 'IBM Plex Sans'; cursor: pointer; }
.chip:hover { border-color: var(--muted); color: var(--ink); }
.chip:active { transform: scale(.97); }
form { display: flex; gap: 9px; padding: 12px 16px calc(12px + env(safe-area-inset-bottom));
       border-top: 1px solid var(--line); flex-shrink: 0; }
#t { flex: 1; padding: 12px 16px; border: 1px solid var(--line); border-radius: 3px;
     font-size: 15px; font-family: 'IBM Plex Sans'; font-weight: 450; outline: none;
     background: var(--well); color: var(--ink); }
#t::placeholder { color: var(--muted); }
#t:focus { border-color: var(--accent); }
#send { border: 1px solid var(--line); border-radius: 3px; width: 46px; height: 46px;
        background: var(--well); color: var(--ink); font-size: 17px; cursor: pointer; flex-shrink: 0; }
#send:active { transform: scale(.95); }

/* --- plan panel: the only place with structure/state ------------------- */
.peek { display: none; }
.plan-head { padding: 16px 18px 12px; border-bottom: 1px solid var(--line); flex-shrink: 0; }
.plan-title { font-family: 'Anton'; font-weight: 400; font-size: 16px;
              letter-spacing: .06em; text-transform: uppercase;
              background: linear-gradient(120deg, var(--ink) 40%, #b9bcc4 100%);
              -webkit-background-clip: text; background-clip: text; color: transparent; }
.plan-status { font-family: 'JetBrains Mono'; font-size: 10px; letter-spacing: .05em;
               text-transform: uppercase; color: var(--muted); margin-top: 5px; }
.plan-rows { flex: 1; overflow-y: auto; }
.row-day { position: relative; padding: 14px 18px 14px 22px; min-height: 56px;
           border-bottom: 1px solid var(--line); }
.row-day:last-child { border-bottom: none; }
.row-day.today::before { content:""; position: absolute; left: 0; top: 0; bottom: 0;
                          width: 2px; background: linear-gradient(180deg, var(--accent2), var(--accent)); }
.row-day.rest { opacity: .5; }
.row-day.pulse { animation: rowPulse 1000ms ease-out; }
@keyframes rowPulse { 0% { background: linear-gradient(90deg, rgba(224,138,69,.20), rgba(193,96,42,.08) 60%, transparent); }
                       100% { background: transparent; } }
.row-top { display: flex; align-items: baseline; gap: 10px; }
.r-type { font-family: 'JetBrains Mono'; font-size: 10px; letter-spacing: .08em;
          text-transform: uppercase; color: var(--muted); width: 32px; flex-shrink: 0; }
.r-title { font-weight: 500; font-size: 14.5px; color: var(--ink); flex: 1; min-width: 0;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-day.rest .r-title { color: var(--muted); font-weight: 450; }
.r-dur { font-family: 'JetBrains Mono'; font-weight: 600; font-size: 13px; color: var(--data);
         flex-shrink: 0; }
.row-sub { display: flex; gap: 9px; margin-top: 4px; padding-left: 42px; font-size: 12px;
           color: var(--muted); overflow: hidden; }
.row-sub .r-date { flex-shrink: 0; font-family: 'JetBrains Mono'; letter-spacing: .03em; }
.row-sub .r-steps { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-sub .num { font-family: 'JetBrains Mono'; color: var(--data); font-weight: 500; }
.plan-foot { padding: 16px 18px calc(16px + env(safe-area-inset-bottom));
             border-top: 1px solid var(--line); flex-shrink: 0; }
#push { width: 100%; padding: 13px; background: transparent; border: 1px solid var(--accent);
        border-radius: 3px; color: var(--accent); font-weight: 600; font-size: 12.5px;
        font-family: 'JetBrains Mono'; letter-spacing: .08em; text-transform: uppercase;
        cursor: pointer; }
#push:hover:not(:disabled) { background: linear-gradient(90deg, rgba(224,138,69,.10), rgba(193,96,42,.06)); }
#push:active:not(:disabled) { transform: translateY(1px); }
#push.syncing { background: linear-gradient(90deg, var(--accent2), var(--accent)); color: var(--bg); border-color: transparent; }
#push:disabled { opacity: .35; cursor: default; border-color: var(--line); color: var(--muted); }

@media (max-width: 880px) {
  .chat-col { border-right: none; padding-bottom: calc(54px + env(safe-area-inset-bottom)); }
  .plan-col { position: fixed; left: 0; right: 0; bottom: 0; top: auto; height: 82dvh;
              max-width: none; min-width: 0; border-top: 1px solid var(--line);
              transform: translateY(calc(100% - 54px));
              transition: transform .28s cubic-bezier(.4,0,.2,1); z-index: 30; }
  .plan-col.expanded { transform: translateY(0); }
  .peek { display: flex; align-items: center; position: relative; height: 54px;
          padding: 0 18px; flex-shrink: 0; cursor: pointer; border-bottom: 1px solid var(--line); }
  .peek-handle { position: absolute; left: 50%; top: 7px; transform: translateX(-50%);
                 width: 34px; height: 3px; border-radius: 2px; background: var(--line); }
  .peek-text { font-size: 13px; color: var(--ink); flex: 1; padding-top: 5px; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .peek-text .num { font-family: 'JetBrains Mono'; color: var(--data); font-weight: 600; }
  .peek-chev { color: var(--muted); font-size: 11px; padding-top: 5px; flex-shrink: 0;
               transition: transform .28s; }
  .plan-col.expanded .peek-chev { transform: rotate(180deg); }
}
</style></head><body>
<header>
  <div class="hname">Jim<i>.</i></div>
  <div class="htag">training coach</div>
  <a href="#" id="clear">Clear</a>
</header>
<div class="main">
  <div class="chat-col">
    <div id="log"></div>
    <form id="f">
      <input id="t" placeholder="Message Jim…" autocomplete="off">
      <button id="send" type="submit" aria-label="Send">↑</button>
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
const key = new URLSearchParams(location.search).get("key") || "";
const log = document.getElementById("log"), t = document.getElementById("t");
const planCol = document.getElementById("planCol"), peek = document.getElementById("peek");
const peekText = document.getElementById("peekText");
const planRows = document.getElementById("planRows"), planStatus = document.getElementById("planStatus");
const pushBtn = document.getElementById("push");
const KIND = { strength:"STR", conditioning:"COND", mobility:"PT", rest:"REST" };
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const rowSig = new Map();

function bubble(role, node) {
  const row = document.createElement("div"); row.className = "row " + role;
  if (role === "bot") { const av = document.createElement("div"); av.className = "avatar"; row.appendChild(av); }
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
function isoLocal(d) {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
function fmtWhen(iso) {
  const d = new Date(iso + "T00:00");
  return isNaN(d) ? iso : `${DOW[d.getDay()]} ${d.getMonth()+1}/${d.getDate()}`;
}
function buildWeek(draft) {
  const today = new Date(), days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
    const iso = isoLocal(d);
    days.push({ iso, label: fmtWhen(iso), entry: (draft || []).find(x => x.for_date === iso) || null, isToday: i === 0 });
  }
  return days;
}
function nowHM() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }

function renderPlan(draft, opts) {
  const pulseOn = !opts || opts.pulse !== false;
  const days = buildWeek(draft);
  planRows.innerHTML = "";
  let nextPeek = null;
  for (const day of days) {
    const entry = day.entry, isRest = !entry || entry.kind === "rest";
    const sig = entry ? JSON.stringify([entry.kind, entry.title, entry.est_duration_min, entry.steps]) : "REST";
    const prevSig = rowSig.get(day.iso);
    const changed = pulseOn && prevSig !== undefined && prevSig !== sig;
    rowSig.set(day.iso, sig);

    const typeLabel = isRest ? "—" : (KIND[entry.kind] || "—");
    const title = isRest ? "Rest" : entry.title;
    const dur = (!isRest && entry.est_duration_min) ? `${Math.round(entry.est_duration_min)}m` : "";
    const steps = (!isRest && entry.steps && entry.steps.length) ? entry.steps.map(stepLine).join(" &middot; ") : "";

    const row = document.createElement("div");
    row.className = "row-day" + (day.isToday ? " today" : "") + (isRest ? " rest" : "") + (changed ? " pulse" : "");
    row.innerHTML =
      `<div class="row-top"><span class="r-type">${typeLabel}</span>` +
      `<span class="r-title">${esc(title)}</span><span class="r-dur">${dur}</span></div>` +
      `<div class="row-sub"><span class="r-date">${day.label}</span>` +
      (steps ? `<span class="r-steps">${steps}</span>` : "") + `</div>`;
    if (changed) row.addEventListener("animationend", () => row.classList.remove("pulse"), { once: true });
    planRows.appendChild(row);
    if (!isRest && !nextPeek) nextPeek = `${day.label} &middot; ${esc(title)} &middot; <span class="num">${dur}</span>`;
  }
  peekText.innerHTML = nextPeek || "Nothing planned this week";
  const hasPlan = (draft || []).length > 0;
  pushBtn.disabled = !hasPlan;
  pushBtn.classList.remove("syncing");
  pushBtn.textContent = "Push to Garmin";
  planStatus.textContent = hasPlan ? "DRAFT — not pushed" : "NOTHING PLANNED YET";
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
    settle(busy, data.reply);
    if (data.draft !== null && data.draft !== undefined) renderPlan(data.draft);
  } catch (err) { settle(busy, err.message); }
}
async function load() {
  rowSig.clear();
  try {
    const r = await fetch(`/chat/state?key=${encodeURIComponent(key)}`);
    const s = await r.json();
    if (!r.ok) { add("bot", s.detail || "error"); return; }
    if (!s.history.length) {
      add("bot", "Hey — I'm Jim. Tell me how you're feeling, what you want this week, or a long-term goal.");
      showChips();
    }
    for (const m of s.history) add(m.role === "user" ? "me" : "bot", m.content);
    renderPlan(s.draft, { pulse: false });
  } catch { add("bot", "network error — reload"); }
}
document.getElementById("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = t.value.trim(); if (text) send(text);
});
pushBtn.addEventListener("click", async () => {
  pushBtn.disabled = true; pushBtn.classList.add("syncing"); pushBtn.textContent = "Syncing…";
  try {
    const data = await api("/chat/approve", {});
    add("bot", data.summary);
    pushBtn.classList.remove("syncing"); pushBtn.textContent = "Push to Garmin";
    planStatus.textContent = "ON WATCH · synced " + nowHM();
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


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> str:
    _check_key(key)
    return CHAT_PAGE
