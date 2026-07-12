# Jim

A personal training agent (single user). Every night it reviews what actually
happened (Garmin + a read-only Notion habit log), reasons about tomorrow
within joint constraints, and drops a proposal into **Jim's chat** — a
lightweight self-hosted chat where you iterate on the plan (or the whole
week), keep long-term goals in plain language, and push to Garmin with one
button. Chat-approved days are scheduled on the watch; the nightly run never
overrides them.

Full design: [PLAN.md](PLAN.md) (agent originally code-named Vesper).
Milestone status:

- [x] **M1** — Garmin write round-trip: verified server-side + on-watch
      (docs/garmin_strength.md, exercise taxonomy verified against Garmin's own)
- [x] **M2** — State layer as tools (`jim/tools/`, fixture-tested, `scripts/backfill.py`)
- [x] **M3** — Bounded agent loop, propose-only (`jim/agent/loop.py`) + nightly reconcile
- [x] **M4** — Gated research + tier escalation (`jim/tools/research.py`, `agent/heuristics.py`)
- [ ] **M5** — Eval suite gating `AUTO_PUSH` (`evals/run_evals.py` scaffold; needs live-compose scenarios)

Interactive surface: **the chat** (docs/chat.md). Memory model incl. long-term
goals: docs/memory.md.

## Layout

```
src/jim/
  config.py          # PLAN §8 constants + env-backed secrets
  schemas.py         # typed tool contracts (PLAN §7)
  db.py              # Postgres + idempotent migrations + kv store
  tools/             # garmin, notion (read-only), history, research (gated), memory
  agent/
    heuristics.py    # off-heuristic (gates research) + tier escalation
    compose.py       # the one generative step: state -> StructuredSession JSON
    validate.py      # deterministic guardrail + conservative fallback
    loop.py          # run_agent: bounded, injectable toolbox
  jobs/              # nightly run (single cron: reconcile today + plan tomorrow)
  playbook.py        # loads playbook/ (base workouts + PT + directives) into context
  coach.py           # Jim's chat: iterate on drafts, goals memory, approve -> Garmin
  app.py             # thin FastAPI (health + manual trigger + /chat)
playbook/            # editable memory: base_workouts.yaml, pt_routines.yaml, directives.md
migrations/          # additive, idempotent SQL (PLAN §6 + kv/chat)
scripts/             # m1_roundtrip.py (live), backfill.py (live), seed_corpus.py
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
python -m jim.jobs.nightly        # the nightly run: reconcile today + plan tomorrow
uvicorn jim.app:app --reload      # local service; chat at /chat?key=<CHAT_SECRET>
python scripts/backfill.py 90     # backfill Garmin history into Postgres
```

## Deploy

**[DEPLOY.md](DEPLOY.md)** — Vercel serves the chat (`vercel.json` +
`api/index.py`), Neon is the database, and Vercel Cron hits `/api/cron/nightly`.
Then install the chat to your phone's home screen as a PWA.

Three things that bite if you skip the guide. Serverless has no reliable startup
hook, so migrations run on the request path (`db.ensure_migrated()`), not at boot.
A function can't do a Garmin SSO login — no stdin to answer MFA — so it uses a
`GARMIN_TOKENS` session blob (`python scripts/garmin_login.py --export`). And the
nightly must finish inside the function's `maxDuration`; the endpoint returns
`elapsed_sec` so you can watch for it.
