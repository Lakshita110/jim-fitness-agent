# Vesper

A nightly training agent (single-user personal tool). At end of day it reviews
what actually happened (Garmin + Notion), reasons about tomorrow within joint
constraints, optionally researches, and proposes tomorrow's session — pushing
structured workouts to Garmin Connect on approval.

Full design: [PLAN.md](PLAN.md). Milestone status:

- [ ] **M1** — Garmin write round-trip (`scripts/m1_roundtrip.py`, needs real creds/watch)
- [x] **M2** — State layer as tools (`vesper/tools/`, fixture-tested, `scripts/backfill.py`)
- [x] **M3** — Bounded agent loop, propose-only (`vesper/agent/loop.py`) + morning reconcile
- [x] **M4** — Gated research + tier escalation (`vesper/tools/research.py`, `agent/heuristics.py`)
- [ ] **M5** — Eval suite gating `AUTO_PUSH` (`evals/run_evals.py` scaffold; needs live-compose scenarios)

M2–M4 are implemented and unit-tested offline; the Notion schema mapping is
wired to the real workspace databases (see `docs/notion_schema.md`), while the
Garmin live path (auth + strength JSON, backfill) still needs verification —
M1 is deliberately the first thing to run.

## Layout

```
src/vesper/
  config.py          # PLAN §8 constants + env-backed secrets
  schemas.py         # typed tool contracts (PLAN §7)
  db.py              # Postgres + idempotent migration runner
  tools/             # garmin, notion, history (deterministic), research (gated), memory
  agent/
    heuristics.py    # off-heuristic (gates research) + tier escalation
    compose.py       # the one generative step: state -> StructuredSession JSON
    validate.py      # deterministic guardrail + conservative fallback
    loop.py          # run_agent: bounded, injectable toolbox
  jobs/              # nightly run + morning reconcile (Render Cron entrypoints)
  app.py             # thin FastAPI (health + manual trigger)
migrations/          # additive, idempotent SQL (PLAN §6)
scripts/             # m1_roundtrip.py (live), backfill.py (live)
evals/               # M5 scaffold: plan quality / tool use / cost
tests/               # offline only — recorded fixtures, no live APIs
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env    # fill in secrets — never commit .env
```

## Verify

```bash
ruff check .
pytest
python evals/run_evals.py
```

## Run

```bash
python scripts/m1_roundtrip.py        # M1: put one hardcoded workout on the watch
python scripts/backfill.py 90        # M2: backfill Garmin history into Postgres
python -m vesper.jobs.nightly        # the nightly agent run
python -m vesper.jobs.reconcile      # the morning adherence job
uvicorn vesper.app:app --reload      # local service
```

Deploy: `render.yaml` (web service + two cron jobs; secrets via the
`vesper-secrets` env group). Cron schedules are in UTC — see the comment there.
