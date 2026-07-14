# PLAN.md — Vesper (working title)

> A nightly training agent. At end of day it reviews what actually happened
> (Garmin + Notion), reasons about tomorrow within my joint constraints,
> optionally researches, and proposes tomorrow's session — pushing structured
> workouts to Garmin on approval.
>
> Rename freely — "Vesper" is a placeholder (evening / nightly).

> [!NOTE]
> **This is the original design record, kept for the reasoning.** The agent
> shipped as **Jim**, and five things went differently. For what the system
> actually is today, read `CLAUDE.md`.
>
> | PLAN said | Reality |
> |---|---|
> | Deploy on Render (web + cron) | **Vercel** serverless + **Neon** Postgres; the nightly is an HTTP endpoint (`/api/cron/nightly`), not a process (DEPLOY.md) |
> | "No web UI in v1 — Notion is the interface" | **Jim's chat** is the interface (`coach.py`, docs/chat.md). Notion is **read-only** and supplies only the habits/knee log; Jim never writes to it |
> | Proposals written to a Notion DB | Proposals land as the **chat draft** in the kv store |
> | Guardrail includes a weekly volume cap (§7) | **Dropped.** Hard rules are safety only; balance across muscle groups is *advisory* — see `agent/validate.py` for why |
> | Nightly + a separate morning reconcile job | **One** nightly run: reconcile today, then plan tomorrow |
>
> Still true: the cost discipline (§4), the one-generative-step design, gated
> research, tier escalation, propose-only until M5 evals gate `AUTO_PUSH`.

---

## 1. Purpose

Replace ad-hoc "what should I train tomorrow?" decisions with a bounded nightly
agent that:

1. Gathers everything done today (activities, recovery, pain/PT log, habits).
2. Reasons about tomorrow given progression, recovery, and knee/ankle limits.
3. Researches only when something is off (pain spike, needed substitution, deload).
4. Proposes a structured session to Notion for morning review, and pushes it to
   Garmin Connect on approval.

This is a **single-user personal tool** (me), not a product.

---

## 2. Goals / Non-goals

**Goals**
- Real agent: cheap model + bounded tool-calling loop, not a fixed pipeline.
- Cheap at runtime. Heavy computation is deterministic Python; the LLM only
  composes the session and orchestrates tools.
- Covers all training (strength + conditioning), not just knees.
- Can create and schedule structured Garmin workouts that sync to my watch.
- Closes the loop: tracks what I actually did vs. what it suggested.
- Doubles as pre-Databricks practice: RAG, tool use, memory, agent evals.

**Non-goals (deferred)**
- No web UI in v1 — Notion is the interface.
- No auto-push to the watch until evals pass — propose-only first.
- No multi-user / auth beyond my own credentials.
- No open-web research crawl — curated corpus only.
- No mobile app, no notifications beyond the Notion write in v1.

---

## 3. Explicit assumptions

- Dev on Windows/WSL; deploy on Render (web service + Cron job). PowerShell
  conventions per `~/.claude/CLAUDE.md`.
- Postgres on Render (with `pgvector`). One database, additive migrations.
- Secrets in env vars (never committed): Garmin creds, Notion token, OpenRouter
  key, Tavily key, DB URL.
- Garmin access is via `python-garminconnect` (community lib, mobile-SSO auth).
  There is **no** self-serve public Garmin API. Tokens cache at
  `~/.garminconnect`; MFA may be prompted on first/expired login.
- My Garmin device supports structured + strength workouts.
- Notion already has: Tasks, Expenses, Habits+Knee Log, Networking. I will supply
  the database IDs and the exact property names for the Knee Log + Tasks DBs.
- "End of day" = my local timezone; Cron fires ~21:00 local. Timezone is config.
- All inference via OpenRouter using named tier constants (no direct provider SDKs).

**Open questions to resolve before M2** (see §12): Knee Log property schema,
which Garmin device/model, exact strength-workout JSON accepted by my account.

---

## 4. Architecture

**Two model roles, kept separate:**
- **Build-time = Fable (Claude Code).** Writes the code. Not called at runtime.
- **Runtime = cheap OpenRouter tier.** Runs the agent loop + composes sessions.

**Cost discipline — the LLM does as little as possible:**
- Deterministic Python/SQL computes weekly volume, muscle-group balance,
  days-since-legs, pain trend, recovery signals. These live *inside* the read
  tools, so the model sees compact summaries, never raw rows.
- The model is only truly generative at one step: turning
  `{state + goals + constraints}` into a structured week as Garmin-schema JSON.
- Research is **gated**: a cheap heuristic decides if anything's "off"; only then
  is the model allowed to call the research tool.
- **Tier escalation for the agent itself**: routine nights run the cheap tier;
  escalate to the quality tier only when state is flagged ambiguous.

**Control flow (nightly, bounded):**
```
Render Cron (~21:00 local)
  └─> run_agent()  # single-shot, MAX_TOOL_CALLS cap
        1. get_garmin_today()        # read
        2. get_notion_logs()         # read
        3. query_history()           # read (deterministic features)
        4. [heuristic] anything off? → maybe research_training()  # gated
        5. compose_session()         # cheap LLM → structured JSON
        6. validate(session)         # deterministic guardrail
        7. write_notion(proposal + rationale)   # propose-only in v1
        8. record_suggestion()       # memory/outcome table
        (stop; hard cap on tool calls)
```
Next morning a separate lightweight job reads Garmin actuals and reconciles them
against the stored suggestion (adherence), feeding the last-7 summary back in.

---

## 5. Tech stack

- **Language:** Python 3.12, type hints throughout, f-strings, `pathlib`.
- **Service:** FastAPI (thin — trigger endpoint + health check). Agent logic is a
  callable, not tied to HTTP.
- **Scheduler:** Render Cron (nightly run + morning reconcile).
- **DB:** Postgres + `pgvector`.
- **Garmin:** `python-garminconnect` (read metrics + typed workout create/schedule).
- **Inference:** OpenRouter, tiered constants (`MODEL_FAST`, `MODEL_QUALITY`).
- **Research:** Tavily + `pgvector` over a curated corpus (vetted articles + my PT
  protocol).
- **Notion:** official API (read logs/tasks, write proposals).
- **Verify:** `ruff` + `pytest` (defaults from my global CLAUDE.md).

---

## 6. Data model (Postgres — additive)

```
garmin_daily        # one row/day: date, hrv, sleep, body_battery, readiness, resting_hr, raw JSON
garmin_activities   # activity_id PK, date, type, duration, load, summary JSON
notion_daily_log    # date, pain_level, pain_location, pt_done (bool), habits JSON, day_score
features_daily      # date, weekly_volume, muscle_group_balance JSON, days_since_legs, pain_trend
suggestions         # id, run_ts, for_date, plan JSON, rationale, research_used bool, model_tier
outcomes            # suggestion_id FK, actual_activity_id, adhered bool, notes, reconciled_ts
research_corpus      # id, source, title, chunk_text, embedding vector, tags
```
Keep migrations additive and idempotent. `raw JSON` columns preserve full API
payloads so features can be recomputed without re-fetching.

---

## 7. Tool contracts (the agent's tools)

Signatures are the contract Claude Code should implement and unit-test in
isolation. Each returns a compact dict/summary, not raw dumps.

```python
def get_garmin_today(day: date) -> GarminToday:
    """Activities + recovery for `day`. Computation done here; returns summary."""

def get_notion_logs(day: date) -> NotionDay:
    """Pain level/location, PT adherence, habits, and tomorrow's planned tasks."""

def query_history(as_of: date, window_days: int = 28) -> HistoryFeatures:
    """Deterministic features: weekly volume, muscle-group balance,
    days_since_legs, pain_trend. Pure SQL/Python, no LLM."""

def research_training(question: str, k: int = 5) -> list[ResearchHit]:
    """Tavily + pgvector over the curated corpus. GATED — only callable when the
    off-heuristic fires. Returns grounded snippets with sources."""

def create_garmin_workout(session: StructuredSession) -> WorkoutRef:
    """Create a structured workout via the workout API (JSON path, NOT FIT upload)."""

def schedule_workout(workout_id: str, on: date) -> None: ...

def write_notion(for_date: date, plan: StructuredSession, rationale: str) -> None:
    """Write the proposal + reasoning to Notion for morning review."""

def record_suggestion(for_date: date, plan: StructuredSession,
                      rationale: str, research_used: bool, tier: str) -> int: ...
```

**Guardrail `validate(session)`** (deterministic, runs before any Garmin write):
knee/ankle constraints respected? progression sane vs. history? weekly volume in
bounds? step count under Garmin's max? Reject → agent revises or falls back.

---

## 8. Config constants

```python
MODEL_FAST    = "..."   # cheap OpenRouter tier — default agent + compose
MODEL_QUALITY = "..."   # escalation only, on ambiguous state
MAX_TOOL_CALLS = 8      # hard cap per nightly run
RESEARCH_ENABLED = True # gated behind off-heuristic regardless
AUTO_PUSH = False       # propose-only until evals pass (M5)
CRON_LOCAL_HOUR = 21
TIMEZONE = "..."        # my local tz
```

---

## 9. Milestones

Each ships something usable. Do them in order; M1 de-risks the biggest unknown.

### M1 — Write round-trip (de-risk strength JSON) ⚠️ riskiest first
Auth to Garmin; create + schedule **one hardcoded strength session**; confirm it
syncs to the watch.
- **Why first:** the typed workout classes are cardio-oriented; strength/lifting
  likely needs a hand-built JSON payload, and the write path is the workout API
  (JSON), not FIT upload (FIT structured-workout upload is rejected, 406). Prove
  this before building anything on top.
- **Ships:** a workout on my wrist I didn't build by hand.
- **Done when:** a scheduled strength workout appears on the watch after sync, and
  the exact accepted JSON shape is documented in `docs/garmin_strength.md`.

### M2 — State layer as tools
Implement `get_garmin_today`, `get_notion_logs`, `query_history` over Postgres;
backfill ~90 days; compute `features_daily`. No LLM.
- **Ships:** callable, unit-tested tools + a readiness/volume view.
- **Done when:** each tool returns a correct compact summary for a given date, with
  tests using recorded fixtures (no live API in CI).

### M3 — The agent loop (propose-only)
Bounded tool-calling run on `MODEL_FAST`: reads state → `compose_session` →
`validate` → `write_notion` → `record_suggestion`. Add the morning reconcile job
writing `outcomes`. No research, no auto-push.
- **Ships:** a real nightly agent that proposes tomorrow to Notion and tracks
  adherence.
- **Done when:** a full nightly run produces a validated proposal in Notion under
  the tool-call cap, and next morning's job reconciles it.

### M4 — Research + escalation
Add curated `research_corpus` + `research_training` (Tavily + pgvector), gated by
the off-heuristic. Add cheap→quality tier escalation on ambiguous state.
- **Ships:** adaptive suggestions with citations when something's off.
- **Done when:** a normal night skips research; an injected pain-spike/substitution
  scenario triggers exactly one research call and a grounded, cited change.

### M5 — Agent evals (the Databricks hook)
Eval suite grading three axes: **plan quality** (constraints honored, progression
sane, volume in bounds), **tool-use correctness** (right tools called, research
skipped when unneeded), **trajectory cost** (tool calls, tokens). Gate `AUTO_PUSH`
on passing.
- **Ships:** an eval suite that gates the agent; flip `AUTO_PUSH = True` once green.
- **Done when:** eval runs on a fixed scenario set, reports per-axis scores, and
  fails the build if plan-quality drops below threshold.

---

## 10. Known risks / caveats

- **Strength workout JSON** is the main unknown — hence M1. Cardio has clean typed
  helpers; lifting may need custom payload shaping.
- **Garmin auth fragility**: mobile-SSO, occasional MFA, token expiry. Cache tokens;
  handle re-login; never hardcode creds.
- **Research quality**: keep the corpus curated; do not let the agent free-roam the
  open web. Everything cited.
- **Runtime cost creep**: enforce `MAX_TOOL_CALLS`, keep tool outputs compact, keep
  research gated. Log tokens per run.
- **Safety of auto-push**: stays off until M5 passes.

---

## 11. Environment

```
GARMIN_EMAIL, GARMIN_PASSWORD
NOTION_TOKEN, NOTION_KNEE_LOG_DB_ID, NOTION_TASKS_DB_ID, NOTION_PROPOSAL_DB_ID
OPENROUTER_API_KEY
TAVILY_API_KEY
DATABASE_URL
APP_TIMEZONE
```

## 12. Open questions (resolve before M2)

1. Exact Knee+Habit Log property names/types (for `get_notion_logs` mapping).
2. Garmin device model — confirm structured + strength workout support.
3. Where proposals live in Notion: new "Training Proposals" DB or a view on Tasks?
4. Curated corpus seed list for `research_corpus` (my PT protocol + vetted sources).

## 13. Verification

- `ruff check .` and `pytest` green before any milestone is "done".
- Live Garmin/Notion/OpenRouter calls mocked in CI via recorded fixtures.
- Each tool has isolation tests before it's wired into the loop.
