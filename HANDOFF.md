# Running Jim locally

How to bring Jim up on a fresh machine so the app runs against live
Garmin/OpenRouter in a real browser, plus the things that bit us on the way.

For the design, read `CLAUDE.md` (architecture) and `docs/` (chat, memory,
garmin_strength, notion_schema). `PLAN.md` is the original design record.

## What Jim is

A single-user personal training agent. Nightly it reviews the day (Garmin + a
read-only Notion habit/knee log), reasons about tomorrow within knee/ankle
constraints, and drops a proposal into **Jim's chat** — a self-hosted page where
you iterate on the plan and push structured workouts to Garmin on approve.
Python 3.11+, FastAPI, Postgres, OpenRouter (via the `openai` SDK),
`garminconnect`. No build step — the whole chat UI is one inline HTML string in
`src/jim/app.py`.

Work happens on `main` (github.com/Lakshita110/jim-fitness-agent).
`AUTO_PUSH=False`: the nightly job is propose-only, and workouts reach the watch
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
- `NOTION_TOKEN` — optional; leave blank to run without it (the Pain card and
  Notion context just hide; everything else works).
- `DATABASE_URL=postgresql://jim:jim@localhost:5432/jim`
- `CHAT_SECRET` — any long random string (goes in the chat URL once).
- `APP_TIMEZONE` — yours.

`CRON_SECRET` and `GARMIN_TOKENS` are for the serverless deploy only (DEPLOY.md);
locally you can leave both blank.

Verify, then run:

```bash
ruff check .
pytest                            # 138 tests, all offline
python scripts/backfill.py 120    # first run: pull ~120d of Garmin history
uvicorn jim.app:app --reload
```

Open **http://127.0.0.1:8000/chat?key=YOUR_CHAT_SECRET**. The key sets a session
cookie, so subsequent visits don't need it. Try "plan my week" or "my knee is
sore today".

## Gotchas learned the hard way

- **Garmin login** is token-based; tokens cache at `~/.garminconnect`. From a
  normal residential IP the login works directly (datacenter IPs get blocked by
  Cloudflare — which is exactly why the serverless deploy uses a `GARMIN_TOKENS`
  blob instead). If login fails with a transport/`curl_cffi` error,
  `pip uninstall curl_cffi` so it falls back to plain `requests`.
- **Notion API** needs `notion-client` 3.x — queries go through
  `data_sources.query`, not the old `databases.query` (handled in
  `src/jim/tools/notion.py`). Notion is **read-only** by design, and only the
  habits/knee log is read.
- **pgvector**: migration `002_research_corpus.sql` is skipped with a warning if
  the `vector` extension isn't installed. That only disables the research corpus;
  everything else runs.
- **`fetch_state` degrades per-source** — a down integration (e.g. no Notion
  token) won't blank Garmin/readiness; the affected cards just hide.
- **State is cached for an hour.** After a backfill, clear it or the cards keep
  showing stale "no data": `python -c "from jim.db import kv_set; kv_set('state', None)"`.
- **Nothing reaches the watch unattended.** Only the chat's push buttons
  (`coach.approve` / `coach.push_day`) schedule workouts.

## Backlog

- **M5 eval suite** (`evals/run_evals.py` is a scaffold) — needs live-compose
  scenarios. Passing it is what would gate flipping `AUTO_PUSH` on.
- Readiness card loads once per page load rather than after every message.
  Fine in practice: load and recovery don't move within a session.
