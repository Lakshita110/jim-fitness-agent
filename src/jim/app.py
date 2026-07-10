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
<meta name="theme-color" content="#13150F">
<title>Jim</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,500;1,9..144,600&family=Inter:wght@400;450;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#13150F; --panel:rgba(31,34,26,.72); --panel-solid:#1C1F17; --line:#2C3025;
  --ink:#ECE9E0; --muted:#98A08C; --sage:#B4CE9E; --sage-dim:#8FAE78;
  --data:#E7B57C; --user:rgba(50,56,42,.85);
}
* { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
body { font-family: 'Inter', -apple-system, system-ui, sans-serif;
       background:
         radial-gradient(1200px 560px at 8% -10%, rgba(180,206,158,.10) 0%, transparent 60%),
         radial-gradient(1000px 520px at 112% 4%, rgba(231,181,124,.09) 0%, transparent 58%),
         linear-gradient(180deg, #171A12 0%, var(--bg) 42%);
       color: var(--ink); height: 100dvh; display: flex; flex-direction: column;
       -webkit-font-smoothing: antialiased; overflow: hidden; }
header { padding: 16px 22px 14px; display: flex; align-items: center; gap: 12px;
         z-index: 5; flex-shrink: 0; }
.greet { flex: 1; min-width: 0; }
.greet-line { font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
              font-size: 22px; letter-spacing: -.01em; color: var(--ink); line-height: 1.15; }
.greet-sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; }
.greet-sub b { color: var(--sage); font-weight: 600; }
#clear { color: var(--muted); font-size: 12px; text-decoration: none; flex-shrink: 0; }
#clear:hover { color: var(--ink); }

.main { flex: 1; display: flex; min-height: 0; position: relative; }
.chat-col { flex: 1 1 58%; min-width: 0; display: flex; flex-direction: column;
            border-right: 1px solid var(--line); }
.plan-col { flex: 0 0 42%; max-width: 400px; min-width: 300px; display: flex;
            flex-direction: column; position: relative; }
.plan-col::after { content:""; position: absolute; left: 0; right: 0; bottom: 0; height: 200px;
                   background: radial-gradient(130% 100% at 50% 130%, rgba(180,206,158,.10), transparent 70%);
                   pointer-events: none; z-index: 0; }

/* --- chat: conversation only --------------------------------------------- */
#log { flex: 1; overflow-y: auto; padding: 14px 16px 8px; display: flex;
       flex-direction: column; gap: 11px; }
.row { display: flex; max-width: 86%; gap: 9px; }
.row.me { align-self: flex-end; }
.row.bot { align-self: flex-start; align-items: flex-end; }
.avatar { width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0; margin-bottom: 2px;
          background: radial-gradient(circle at 34% 30%, var(--sage), var(--sage-dim)); }
.msg { padding: 11px 15px; border-radius: 16px; font-size: 14.5px; line-height: 1.55;
       white-space: pre-wrap; word-wrap: break-word; }
.me .msg { background: var(--user); color: var(--ink); border-bottom-right-radius: 5px; }
.bot .msg { background: #1F231A; color: var(--ink); border-bottom-left-radius: 5px; }
.msg.busy { display: flex; gap: 4px; align-items: center; padding: 14px; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted);
       animation: bounce 1.3s infinite; }
.dot:nth-child(2){ animation-delay:.16s } .dot:nth-child(3){ animation-delay:.32s }
@keyframes bounce { 0%,64%,100%{ transform: translateY(0); opacity:.4 }
                    32%{ transform: translateY(-5px); opacity:1 } }
.chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 2px 2px 6px; align-self: flex-start; }
.chip { border: 1px solid var(--line); background: rgba(255,255,255,.02); color: var(--muted);
        border-radius: 999px; padding: 8px 14px; font-size: 12.5px; font-weight: 500;
        font-family: inherit; cursor: pointer; }
.chip:hover { border-color: var(--sage); color: var(--ink); }
.chip:active { transform: scale(.97); }
form { display: flex; gap: 10px; align-items: center;
       padding: 12px 16px calc(14px + env(safe-area-inset-bottom)); flex-shrink: 0; }
#t { flex: 1; padding: 13px 18px; border: 1px solid var(--line); border-radius: 999px;
     font-size: 15px; font-family: inherit; font-weight: 450; outline: none;
     background: var(--panel-solid); color: var(--ink); }
#t::placeholder { color: var(--muted); }
#t:focus { border-color: var(--sage); }
#send { border: none; border-radius: 50%; width: 46px; height: 46px; flex-shrink: 0;
        background: linear-gradient(145deg, var(--sage), var(--sage-dim)); color: #1c2416;
        font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
#send:active { transform: scale(.94); }

/* --- plan panel: the only place with structure/state ------------------- */
.peek { display: none; }
.plan-head { padding: 18px 20px 10px; flex-shrink: 0; position: relative; z-index: 1; }
.plan-title { font-family: 'Fraunces', Georgia, serif; font-style: italic; font-weight: 500;
              font-size: 19px; letter-spacing: -.01em; color: var(--ink); }
.plan-status { font-size: 11px; letter-spacing: .02em; color: var(--muted); margin-top: 4px; }
.plan-rows { flex: 1; overflow-y: auto; padding: 4px 12px; position: relative; z-index: 1; }
.row-day { position: relative; padding: 13px 14px; min-height: 58px; border-radius: 14px;
           margin-bottom: 2px; }
.row-day.today { background: linear-gradient(100deg, rgba(180,206,158,.12), rgba(180,206,158,.03) 72%); }
.row-day.today::before { content:""; position: absolute; left: 0; top: 13px; bottom: 13px;
                          width: 3px; border-radius: 3px; background: var(--sage); }
.row-day.rest { opacity: .45; }
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
.plan-foot { padding: 14px 18px calc(16px + env(safe-area-inset-bottom));
             flex-shrink: 0; position: relative; z-index: 1; }
#push { width: 100%; padding: 14px; background: transparent; border: 1.5px solid var(--sage);
        border-radius: 999px; color: var(--sage); font-weight: 600; font-size: 13.5px;
        font-family: inherit; letter-spacing: .01em; cursor: pointer; }
#push:hover:not(:disabled) { background: rgba(180,206,158,.08); }
#push:active:not(:disabled) { transform: translateY(1px); }
#push.syncing { background: linear-gradient(145deg, var(--sage), var(--sage-dim));
                color: #1c2416; border-color: transparent; }
#push:disabled { opacity: .4; cursor: default; border-color: var(--line); color: var(--muted); }

@media (max-width: 880px) {
  .chat-col { border-right: none; padding-bottom: calc(56px + env(safe-area-inset-bottom)); }
  .plan-col { position: fixed; left: 0; right: 0; bottom: 0; top: auto; height: 82dvh;
              max-width: none; min-width: 0; background: var(--panel-solid);
              border-top: 1px solid var(--line); border-radius: 20px 20px 0 0;
              transform: translateY(calc(100% - 56px));
              transition: transform .28s cubic-bezier(.4,0,.2,1); z-index: 30; }
  .plan-col.expanded { transform: translateY(0); }
  .peek { display: flex; align-items: center; position: relative; height: 56px;
          padding: 0 20px; flex-shrink: 0; cursor: pointer; }
  .peek-handle { position: absolute; left: 50%; top: 8px; transform: translateX(-50%);
                 width: 36px; height: 4px; border-radius: 2px; background: var(--line); }
  .peek-text { font-size: 13px; color: var(--ink); flex: 1; padding-top: 6px; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .peek-chev { color: var(--muted); font-size: 11px; padding-top: 6px; flex-shrink: 0;
               transition: transform .28s; }
  .plan-col.expanded .peek-chev { transform: rotate(180deg); }
}
</style></head><body>
<header>
  <div class="greet">
    <div class="greet-line" id="greetLine">Hello</div>
    <div class="greet-sub" id="greetSub"><b>Jim</b> · your training coach</div>
  </div>
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
const DOW_FULL = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const rowSig = new Map();

function setGreeting() {
  const now = new Date(), h = now.getHours();
  const g = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  const line = document.getElementById("greetLine");
  const sub = document.getElementById("greetSub");
  if (line) line.textContent = g;
  if (sub) sub.innerHTML = `<b>Jim</b> · ${DOW_FULL[now.getDay()]} ${now.getMonth()+1}/${now.getDate()}`;
}

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
  planStatus.textContent = hasPlan ? "Draft · not pushed yet" : "Nothing planned yet";
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
setGreeting();
load();
</script></body></html>"""


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> str:
    _check_key(key)
    return CHAT_PAGE
