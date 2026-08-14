# Jim

A personal training agent, multi-tenant — each signed-up account (email +
password) connects its own Garmin. Claude is the reasoning engine: it talks
to Garmin directly through a Garmin MCP server (`src/jim/mcp_server.py`,
mounted at `/mcp`), reading real history/readiness and writing structured
workouts, reasoning about the next session within that athlete's knee/ankle
constraints — see `skills/jim-coach/SKILL.md` for how it's meant to use those
tools; there's no code-enforced guardrail behind them, so that skill is the
safety layer. Nightly housekeeping keeps history fresh (Garmin sync,
adherence reconcile, stale-workout cleanup) but never writes a plan itself,
and nothing reaches the watch except through an explicit write-tool call.

This is a backend-only API — no HTML/frontend is included in this repo. A UI
is expected to be built separately against the endpoints below, and an
MCP-capable client (Claude, via a connector) is the coaching surface.

Architecture: **[CLAUDE.md](CLAUDE.md)** (start here) and
[docs/architecture.md](docs/architecture.md). Milestone status:

- [x] **M1** — Garmin write round-trip: verified server-side + on-watch
      (docs/garmin_strength.md, exercise taxonomy verified against Garmin's own)
- [x] **M2** — State layer as tools (`jim/tools/`, fixture-tested, `scripts/backfill.py`)
- [x] **M3** — Chat-driven planning, propose-only, + nightly housekeeping (retired,
      see M6)
- [x] **M4** — Gated research (`jim/tools/research.py`): pgvector corpus search
      + domain-restricted Tavily, now exposed as the `research_training` MCP
      tool. Corpus is shared across every athlete (general training science,
      not any one athlete's protocol) and has a handful of seed docs; fuller
      curation is ongoing — see `data/corpus/README.md`
- [x] **M6** — Garmin MCP server (`src/jim/mcp_server.py`) so Claude can be the
      coach directly, reasoning against real Garmin reads/writes. Verified
      end-to-end; the old `coach.py`/`playbook.py`/`agent/validate.py` chat path
      it replaced has been removed. Safety now lives in
      `skills/jim-coach/SKILL.md`, since there's no code guardrail behind MCP
      tool calls
- [x] **M7** — Cross-user learning without self-editing the safety layer: a
      `technical_notes` table + `report_technical_issue` MCP tool let any
      session log tool-usage/system mistakes (a Garmin quirk, an
      exercise-matching miss), read back via `research_training(domain=
      "technical")` — folded into the existing tool rather than adding a
      separate `get_*` one, to keep the total tool count down (17, not 18).
      Kept deliberately separate (data-wise) from `research_corpus`
      (scientific, operator-curated only) and `constraints` (one athlete's
      own limits). `skills/jim-coach/SKILL.md` itself stays operator-edited
      — see CLAUDE.md's "three memory stores" note

Intensity is steered by a readiness read (acute:chronic workload ratio +
recovery → push/steady/ease/rest, `tools/history.py`), surfaced to the model
via `get_readiness`.

## Layout

```
src/jim/
  config.py          # constants + env-backed secrets
  schemas.py         # typed tool contracts, incl. StructuredSession
  db.py              # Postgres + idempotent migrations + kv store (composite user_id, key)
  migrations/        # additive, idempotent SQL (001-013); ships inside the package
  auth.py            # email+password signup/login, session cookies + bearer tokens, _require_user
  crypto.py          # AES-GCM encrypt/decrypt for Garmin creds at rest
  tools/             # garmin, history, research (gated), memory, exercise_match
  jobs/              # nightly.py (per-user sync + reconcile + cleanup, fanned out; no planning)
                      #   + reconcile.py
  mcp_server.py       # Garmin MCP — read history/readiness/calendar/library, write/schedule, constraints, research, technical_notes
  app.py             # FastAPI app + health, /api/cron/nightly, mounts /mcp — wires in web/
  web/               # route groups (JSON only): auth, garmin onboarding, constraints, deps
api/index.py         # Vercel entrypoint — re-exports app.app as the ASGI handler
skills/jim-coach/    # operating instructions for Claude when it's the coach calling MCP tools
data/corpus/         # curated research corpus (seeded by scripts/seed_corpus.py)
docs/                # architecture, garmin_strength
scripts/             # backfill.py, backfill_users.py, garmin_login.py, seed_corpus.py, exercise_map.py, refresh_garmin_exercises.py
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
```

## Run

```bash
python -m jim.jobs.nightly        # nightly housekeeping: sync + reconcile + cleanup
uvicorn jim.app:app --reload      # local service; JSON API at /auth, /api/*, /settings/garmin, MCP at /mcp
python scripts/backfill.py 90     # backfill Garmin history into Postgres
```

`POST /auth/signup` or `/auth/login` returns both a session cookie and a
bearer token; point an MCP-capable client at `/mcp` with
`Authorization: Bearer <token>` (or `?token=<token>` on the connector URL) to
talk to Jim as the coach.

## Deploy

**[DEPLOY.md](DEPLOY.md)** — Vercel serves the API (`vercel.json` +
`api/index.py`), Neon is the database, and Vercel Cron hits `/api/cron/nightly`.

Three things that bite if you skip the guide. Serverless has no reliable startup
hook, so migrations run on the request path (`db.ensure_migrated()`), not at boot.
A function can't do a Garmin SSO login — no stdin to answer MFA — so it uses a
`GARMIN_TOKENS` session blob (`python scripts/garmin_login.py --export`). And the
nightly must finish inside the function's `maxDuration`; the endpoint returns
`elapsed_sec` so you can watch for it.
