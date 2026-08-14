# Running Jim locally

How to bring Jim up on a fresh machine so the app runs against live
Garmin/OpenRouter, plus the things that bit us on the way.

For the design, read `CLAUDE.md` (architecture) and `docs/architecture.md` +
`docs/garmin_strength.md`.

## What Jim is

A personal training agent, multi-tenant: each signed-up account connects its
own Garmin and gets its own nightly housekeeping run. Claude is the reasoning
engine — it talks to Garmin directly through the MCP server at `/mcp`
(`src/jim/mcp_server.py`), reading real history/readiness and writing
structured workouts, reasoning about the next session within the athlete's
knee/ankle constraints. There's no separate coach model or chat loop behind
it, and no code-enforced guardrail — `skills/jim-coach/SKILL.md` is the
safety layer instead (constraints-first, ground every recommendation in real
data, never write to Garmin without an explicit ask). Nightly housekeeping
just keeps history fresh — it never writes a plan itself.
Python 3.11+, FastAPI, Postgres, OpenRouter (via the `openai` SDK),
`garminconnect`, `fastmcp`. Backend-only: there is no HTML/frontend in this
repo — the old inline chat UI was scrapped, and a new UI is expected to be
built separately against the JSON API.

Work happens on `main` (github.com/Lakshita110/jim-fitness-agent). Nothing
auto-schedules — workouts reach the watch only through an explicit MCP
write-tool call.

## Setup

Prereqs: **Git**, **Python 3.11+**, **PostgreSQL 16**. On Windows use `winget` or
the direct installers (git-scm.com; python.org — tick "Add python.exe to PATH";
postgresql.org — remember the `postgres` superuser password, keep port 5432).

```bash
git clone https://github.com/Lakshita110/jim-fitness-agent.git
cd jim-fitness-agent

python -m venv .venv
# macOS/Linux:  . .venv/bin/activate
# Windows PS:   .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Create the database (matches the default `DATABASE_URL` in `.env.example`):

```bash
psql -U postgres -h localhost -c "CREATE USER jim WITH PASSWORD 'jim';"
psql -U postgres -h localhost -c "CREATE DATABASE jim OWNER jim;"
```

Secrets — copy the template and fill it in (**never commit `.env`**):

```bash
cp .env.example .env
```

- `GARMIN_EMAIL`, `GARMIN_PASSWORD` — your Garmin login.
- `OPENROUTER_API_KEY`, `TAVILY_API_KEY`.
- `DATABASE_URL=postgresql://jim:jim@localhost:5432/jim`
- `SESSION_SECRET`, `CREDENTIAL_ENCRYPTION_KEY` — long random strings (auth is
  email+password via `POST /auth/login`, not a shared URL key).
- `APP_TIMEZONE` — yours.

`CRON_SECRET` and `GARMIN_TOKENS` are for the serverless deploy only (DEPLOY.md);
locally you can leave both blank.

Verify, then run:

```bash
ruff check .
pytest                            # 265 tests, all offline
python scripts/backfill.py 120    # first run: pull ~120d of Garmin history
uvicorn jim.app:app --reload
```

`POST http://127.0.0.1:8000/auth/signup` with `{"email", "password"}` to sign
up (or run `python scripts/backfill_users.py` to create the original
athlete's account from the credentials already in `.env`). The response
returns both a session cookie and a bearer token — point an MCP-capable
client (e.g. Claude, via a connector) at `/mcp` with
`Authorization: Bearer <token>` (or `?token=<token>` on the connector URL if
it can't set headers) to talk to Jim as the coach. Follow
`skills/jim-coach/SKILL.md` for how Claude is meant to use those tools.

## Gotchas learned the hard way

- **Garmin login** is token-based; tokens cache at `~/.garminconnect`. From a
  normal residential IP the login works directly (datacenter IPs get blocked by
  Cloudflare — which is exactly why the serverless deploy uses a `GARMIN_TOKENS`
  blob instead). If login fails with a transport/`curl_cffi` error,
  `pip uninstall curl_cffi` so it falls back to plain `requests`.
- **pgvector**: migration `002_research_corpus.sql` is skipped with a warning if
  the `vector` extension isn't installed. That only disables the research corpus;
  everything else runs.
- **Nothing reaches the watch unattended.** Only an explicit MCP write-tool
  call (`create_or_update_workout`, `save_to_library`, `schedule_workout`)
  touches Garmin.

## Backlog

- No eval suite for the MCP path — the old M5 scaffold targeted the retired
  chat loop's compose step, which no longer exists. A useful shape here would
  score a conversation transcript's tool calls, not a single generated plan.
- `tools/memory.record_suggestion`/`jobs/reconcile.py`'s adherence matching
  currently get no new data — nothing on the MCP path calls
  `record_suggestion` the way `coach.py` used to. See CLAUDE.md's Unresolved
  section.
