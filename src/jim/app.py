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
<meta name="theme-color" content="#0F100D">
<title>Jim</title>
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
    <div class="brand-sub">Data-driven training for you</div>
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
const key = new URLSearchParams(location.search).get("key") || "";
const log = document.getElementById("log"), t = document.getElementById("t");
const planCol = document.getElementById("planCol"), peek = document.getElementById("peek");
const peekText = document.getElementById("peekText");
const planRows = document.getElementById("planRows"), planStatus = document.getElementById("planStatus");
const pushBtn = document.getElementById("push");
const KIND = { strength:"STR", conditioning:"COND", mobility:"PT", rest:"REST" };
const KIND_FULL = { strength:"Strength", conditioning:"Conditioning", mobility:"PT / mobility", rest:"Rest" };
const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
const rowSig = new Map();
const openDays = new Set();
let curReadiness = null, curPain = null, serverToday = null;

function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
// Base-workout names carry an em-dash that the model sometimes corrupts to a
// control char (e.g. "PT Day \x7f Home"); restore it so titles read cleanly.
function cleanTitle(s) { return String(s).replace(/[\x00-\x1F\x7F]+/g, " — ").replace(/\s+/g, " ").trim(); }
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
    `<div class="hero-line">I'm Jim — your training coach.</div>` +
    `<div class="hero-sub">I read your Garmin data and plan around your joints. Nothing hits your watch until you push it.</div>` +
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

function stepLine(x) {
  const dose = x.reps ? `<span class="num">${x.sets}×${x.reps}</span>` : `<span class="num">${x.sets}×${x.duration_sec}s</span>`;
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

function detailHtml(entry) {
  let html = "";
  if (entry.steps && entry.steps.length) {
    html += entry.steps.map(x => {
      const note = x.notes ? `<div class="d-note">${esc(x.notes)}</div>` : "";
      return `<div class="d-step">${stepLine(x)}${note}</div>`;
    }).join("");
  }
  if (entry.rationale_summary) html += `<div class="d-why">${esc(entry.rationale_summary)}</div>`;
  return html;
}

function renderPlan(draft, opts) {
  opts = opts || {};
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

    const typeLabel = isRest ? "—" : (KIND[entry.kind] || "—");
    const title = isRest ? "Rest" : cleanTitle(entry.title);
    const dur = (!isRest && entry.est_duration_min) ? `${Math.round(entry.est_duration_min)}m` : "";
    const steps = (!isRest && entry.steps && entry.steps.length) ? entry.steps.map(stepLine).join(" &middot; ") : "";
    const detail = isRest ? "" : detailHtml(entry);
    const clickable = !!detail;
    if (changed && clickable) openDays.add(day.iso);  // auto-open a day that just changed

    const row = document.createElement("div");
    row.className = "row-day" + (day.isToday ? " today" : "") + (isRest ? " rest" : "")
      + (changed ? " pulse" : "") + (clickable ? " clickable" : "") + (openDays.has(day.iso) ? " open" : "");
    row.innerHTML =
      `<div class="row-top"><span class="r-type">${typeLabel}</span>` +
      `<span class="r-title">${esc(title)}</span><span class="r-dur">${dur}</span>` +
      (clickable ? `<span class="r-chev">&rsaquo;</span>` : "") + `</div>` +
      `<div class="row-sub"><span class="r-date">${day.label}</span>` +
      (steps ? `<span class="r-steps">${steps}</span>` : "") + `</div>` +
      (detail ? `<div class="row-detail">${detail}</div>` : "");
    if (clickable) row.addEventListener("click", () => {
      const nowOpen = row.classList.toggle("open");
      if (nowOpen) openDays.add(day.iso); else openDays.delete(day.iso);
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
    body: JSON.stringify({ key, ...body }) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "error");
  return data;
}
async function send(text) {
  removeHero();
  add("me", text); t.value = "";
  const busy = typing();
  try {
    const data = await api("/chat/message", { text });
    settle(busy, data.reply);
    if (data.today) serverToday = data.today;
    if (data.draft !== null && data.draft !== undefined) renderPlan(data.draft, { focus: true });
  } catch (err) { settle(busy, err.message); }
}
async function load() {
  rowSig.clear();
  try {
    const r = await fetch(`/chat/state?key=${encodeURIComponent(key)}`);
    const s = await r.json();
    if (!r.ok) { add("bot", s.detail || "error"); return; }
    curReadiness = s.readiness || null;
    curPain = s.pain || null;
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


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> str:
    _check_key(key)
    return CHAT_PAGE
