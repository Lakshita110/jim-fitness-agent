# Running Jim locally

How to bring Jim up on a fresh machine so the app runs against live
Garmin/OpenRouter in a real browser, plus the things that bit us on the way.

For the design, read `CLAUDE.md` (architecture) and `docs/` (chat, memory,
garmin_strength).

## What Jim is

A personal training agent, multi-tenant: each signed-up account connects its
own Garmin, edits its own playbook, and gets its own nightly housekeeping run.
Plans come from talking to **Jim's chat**, a JSON API (`docs/chat.md`) where
the athlete reasons with Jim about the next session within their knee/ankle
constraints (using real Garmin history) and pushes structured workouts to
Garmin on approve. Nightly housekeeping just keeps that history fresh — it
never writes a plan itself.
Python 3.11+, FastAPI, Postgres, OpenRouter (via the `openai` SDK),
`garminconnect`. Backend-only: there is no HTML/frontend in this repo — the
old inline chat UI was scrapped, and a new UI is expected to be built
separately against the JSON API.

Work happens on `main` (github.com/Lakshita110/jim-fitness-agent).
`AUTO_PUSH=False`: nothing auto-schedules — workouts reach the watch
only through the chat's push buttons.

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
  email+password via `POST /auth/login`, not a shared URL key — see
  `docs/chat.md`).
- `APP_TIMEZONE` — yours.

`CRON_SECRET` and `GARMIN_TOKENS` are for the serverless deploy only (DEPLOY.md);
locally you can leave both blank.

Verify, then run:

```bash
ruff check .
pytest                            # 215 tests, all offline
python scripts/backfill.py 120    # first run: pull ~120d of Garmin history
uvicorn jim.app:app --reload
```

`POST http://127.0.0.1:8000/auth/signup` with `{"email", "password"}` to sign
up (or run `python scripts/backfill_users.py` to create the original
athlete's account from the credentials already in `.env`). Signing in sets a
session cookie, so subsequent requests to `/chat/*` need no key or password.
Try `POST /chat/message` with `{"text": "plan my week"}` or
`{"text": "my knee is sore today"}` — see `docs/chat.md` for the full
endpoint list.

## Gotchas learned the hard way

- **Garmin login** is token-based; tokens cache at `~/.garminconnect`. From a
  normal residential IP the login works directly (datacenter IPs get blocked by
  Cloudflare — which is exactly why the serverless deploy uses a `GARMIN_TOKENS`
  blob instead). If login fails with a transport/`curl_cffi` error,
  `pip uninstall curl_cffi` so it falls back to plain `requests`.
- **pgvector**: migration `002_research_corpus.sql` is skipped with a warning if
  the `vector` extension isn't installed. That only disables the research corpus;
  everything else runs.
- **`fetch_state` degrades per-source** — a down integration won't blank
  Garmin/readiness; the affected cards just hide.
- **State is cached for an hour.** After a backfill, clear it or the cards keep
  showing stale "no data": `python -c "from jim.db import kv_set; kv_set('state', None)"`.
- **Nothing reaches the watch unattended.** Only the chat's push buttons
  (`coach.approve` / `coach.push_day`) schedule workouts.

## Backlog

- **No eval suite gates `AUTO_PUSH` yet.** The old M5 scaffold
  (`evals/run_evals.py`) tested the nightly auto-compose path, which no longer
  exists — a chat-turn eval would need a different shape (one scenario per
  conversation).
- Readiness card loads once per page load rather than after every message.
  Fine in practice: load and recovery don't move within a session.
