# Jim

A personal training agent, multi-tenant — each signed-up account (email +
password) connects its own Garmin and edits its own playbook. Plans come from
talking to **Jim's chat** — a lightweight self-hosted chat where the athlete
reasons with Jim about the next session within their joint constraints (using
real Garmin history + a read-only Notion habit log), iterates on the plan (or
the whole week), keeps long-term goals in plain language, and pushes to Garmin
with one button. Nightly housekeeping keeps that history fresh (Garmin/Notion
sync, adherence reconcile, stale-workout cleanup) but never writes a plan
itself.

Architecture: **[CLAUDE.md](CLAUDE.md)** (start here) and
[docs/architecture.md](docs/architecture.md). [PLAN.md](PLAN.md) is the original
design record — kept for the reasoning, superseded in places (agent was
originally code-named Vesper). Milestone status:

- [x] **M1** — Garmin write round-trip: verified server-side + on-watch
      (docs/garmin_strength.md, exercise taxonomy verified against Garmin's own)
- [x] **M2** — State layer as tools (`jim/tools/`, fixture-tested, `scripts/backfill.py`)
- [x] **M3** — Chat-driven planning, propose-only (`jim/coach.py`) + nightly housekeeping
- [x] **M4** — Gated research (`jim/tools/research.py`), reached via chat's lookup rounds
- [ ] **M5** — Eval suite gating `AUTO_PUSH` — not started; needs a chat-turn eval shape
      (the old nightly-auto-compose scaffold was retired along with that code path)

Interactive surface: **the chat** (docs/chat.md). Memory model incl. long-term
goals: docs/memory.md. Intensity is steered by a readiness read (acute:chronic
workload ratio + recovery → push/steady/ease/rest, `tools/history.py`).

## Layout

```
src/jim/
  config.py          # PLAN §8 constants + guardrail bounds + env-backed secrets
  schemas.py         # typed tool contracts (PLAN §7)
  db.py              # Postgres + idempotent migrations + kv store (composite user_id, key)
  migrations/        # additive, idempotent SQL (001-008); ships inside the package
  auth.py            # email+password signup/login, session cookies, _require_user
  crypto.py          # AES-GCM encrypt/decrypt for Garmin/Notion creds at rest
  static/            # committed PWA icons (no Pillow at runtime)
  tools/             # garmin, notion (read-only), history, research (gated), memory
  agent/
    validate.py      # hard safety guardrail + advisory balance + fallback (used by coach.py)
  jobs/              # nightly.py (per-user sync + reconcile + cleanup, fanned out; no planning)
                      #   + reconcile.py
  playbook.py        # per-account playbook (Postgres JSONB); disk YAML is the signup seed
  coach.py           # Jim's chat: composes drafts, goals memory, approve -> Garmin
  app.py             # FastAPI app + health, /api/cron/nightly, static/manifest, /login —
                      #   wires in web/
  web/               # route groups: auth, chat, playbook, garmin onboarding, deps, templates
api/index.py         # Vercel entrypoint — re-exports app.app as the ASGI handler
playbook/            # editable memory: base_workouts.yaml, pt_routines.yaml, directives.md
data/corpus/         # curated research corpus (seeded by scripts/seed_corpus.py)
docs/                # architecture, chat, memory, garmin_strength, notion_schema
scripts/             # m1_roundtrip.py, backfill.py, backfill_users.py, garmin_login.py, seed_corpus.py
tests/               # offline only — recorded fixtures, no live APIs
```

The guardrail (`agent/validate.py`) splits in two: **hard** rules reject a day
(forbidden movements, session length, Garmin's step cap, leg-day spacing) and
**advisory** balance notes tell the coach when the plan is skewed across
legs/push/pull/core/conditioning. There is deliberately no weekly volume cap.

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
```

## Run

```bash
python -m jim.jobs.nightly        # nightly housekeeping: sync + reconcile + cleanup
uvicorn jim.app:app --reload      # local service; sign in/up at /login, chat at /chat
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
