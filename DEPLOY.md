# Deploy Jim to Vercel (and put it on your phone)

Vercel serves the chat, Neon is the database, and Vercel Cron runs the nightly
job. About 15 minutes end to end.

```
chat + cron ──▶ Vercel (serverless, api/index.py)
database    ──▶ Neon Postgres
watch       ──▶ Garmin (via a session blob, see step 1)
```

## What's different about serverless (read this once)

Vercel has **no long-running process**, so three things work differently than
they would on a normal box, and each is already handled:

- **Migrations run on the request path**, not at startup — Vercel's ASGI adapter
  doesn't reliably run FastAPI's lifespan, so `db.ensure_migrated()` applies them
  once per cold start. You never run a migrate step by hand.
- **The nightly job is an HTTP endpoint** (`/api/cron/nightly`), because Vercel
  Cron only pings a URL. It's protected by `CRON_SECRET`.
- **The filesystem is ephemeral**, so Garmin can't cache a login. See step 1.

---

## 1. Collect the secrets (5 min, on your laptop)

**Chat key** — gates the chat. Generate a long random one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Cron secret** — gates the nightly endpoint. Generate another:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Garmin session blob** — the one that trips people up. A serverless function has
no cached token store and *no stdin to answer an MFA prompt*, so it can't do a
normal Garmin login. It authenticates from a session blob instead:

```bash
python scripts/garmin_login.py --export
```

This reuses your existing local session if there is one (no password re-entry) and
prints a `GARMIN_TOKENS` blob. **Treat it like a password** — paste it straight
into Vercel, never commit it.

## 2. Create the Neon database

In the Vercel dashboard: **Storage → Create Database → Neon**, and connect it to
the project. Vercel injects `DATABASE_URL` automatically — you don't copy a
connection string by hand.

Neon has `pgvector`, so the research corpus (migration `002`) works. If you swap
in a Postgres without it, that one migration is skipped with a warning and
everything else still runs.

## 3. Import the project

**Add New → Project**, pick this repo. Vercel reads `vercel.json`:

| | |
|---|---|
| `api/index.py` | the whole app (ASGI); every path rewrites here |
| `maxDuration` | 60s — see the note below |
| `crons` | `/api/cron/nightly` daily at 20:00 UTC |

`requirements.txt` is a single `.`, which installs the project from
`pyproject.toml` — that's what makes `import jim` work *and* ships the SQL
migrations and app icons inside the bundle.

## 4. Set the environment variables

Project → **Settings → Environment Variables**:

| Key | Value |
|---|---|
| `CHAT_SECRET` | random string from step 1 |
| `CRON_SECRET` | the other random string from step 1 |
| `GARMIN_TOKENS` | the session blob from step 1 |
| `GARMIN_EMAIL` | your Garmin email |
| `NOTION_TOKEN` | from `.env` |
| `NOTION_KNEE_LOG_DB_ID` | `b872f62a28604573980e983be6fd3143` |
| `OPENROUTER_API_KEY` | from `.env` |
| `TAVILY_API_KEY` | from `.env` |
| `APP_TIMEZONE` | `Europe/Berlin` |

`DATABASE_URL` is already there from step 2.

## 5. Deploy and check

```bash
curl https://<your-app>.vercel.app/health          # {"status":"ok"}
```

The first chat request applies the migrations automatically.

## 6. (Optional) Backfill history

A fresh database has no Garmin history, so the readiness card and load ratio start
empty. Run this **locally** against the Neon database (grab its connection string
from the Vercel/Neon dashboard):

```bash
DATABASE_URL="postgres://…neon…" python scripts/backfill.py 90
DATABASE_URL="postgres://…neon…" python scripts/seed_corpus.py
DATABASE_URL="postgres://…neon…" python -c "from jim.db import kv_set; kv_set('state', None)"
```

That last line clears the cached state snapshot — without it the cards keep
showing stale "no data" for up to an hour.

## 7. Put it on your phone 📱

Open the chat **on your phone**:

```
https://<your-app>.vercel.app/chat?key=YOUR_CHAT_SECRET
```

Then install it — it's a real PWA, so it gets its own icon and opens fullscreen
with no browser chrome:

- **iOS / Safari** — Share → **Add to Home Screen**
- **Android / Chrome** — ⋮ → **Install app**

The installed app launches straight into the chat: the key is baked into the
manifest's `start_url`, so you never type it again.

> **The key is the only thing protecting your chat.** Anyone with that URL can talk
> to Jim and push workouts to your watch. Don't paste it anywhere public. To
> rotate: change `CHAT_SECRET` in Vercel, redeploy, re-install on the phone.

---

## ⚠️ The one thing to watch: the nightly timeout

`maxDuration` is **60s** (the Hobby ceiling). The nightly run does a lot — Garmin
sync (activities, sleep, HRV, per-activity exercise sets), reconcile, then the
agent's LLM compose. **If it exceeds 60s it is killed mid-run**, and it'll happen
silently at 2am.

The endpoint returns `elapsed_sec` precisely so you can watch this. After the
first real run, check it:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" \
     https://<your-app>.vercel.app/api/cron/nightly | jq .elapsed_sec
```

- **Under ~40s** — you're fine.
- **Close to 60s** — raise `maxDuration` in `vercel.json` to `300` (needs Vercel
  **Pro**), or move the nightly to a GitHub Actions cron running
  `python -m jim.jobs.nightly`, which has no such limit. Both entrypoints call the
  same `run_nightly()`, so nothing else changes.

Vercel Cron on Hobby also fires only **once per day**, at an approximate time —
fine for a nightly, but it's not a precise scheduler.

## Changing the icon

The home-screen icon is 💪:

```bash
pip install -e ".[dev]"
python scripts/make_icon.py "🏋️"    # any emoji
git add src/jim/static && git commit -m "New icon"
```

The PNGs are committed and served as static bytes, so production needs neither
Pillow nor an emoji font.

## Troubleshooting

**Chat 500s / "no data" everywhere** — the DB was unreachable, so migrations were
skipped. Check `DATABASE_URL`; they retry on the next request.

**Garmin calls fail with an auth error** — the session blob expired. Re-run
`python scripts/garmin_login.py --export` locally and update `GARMIN_TOKENS`. This
is the one thing you'll have to redo periodically.

**`GARMIN_TOKENS is only N chars`** — the blob was truncated when pasted. Paste it
whole; the app fails loudly rather than silently treating it as a file path.

**Cron ran at the wrong hour** — `schedule` in `vercel.json` is UTC. `0 20 * * *`
= 21:00 Berlin in winter, 22:00 in summer. It's deliberately after the training
day; an earlier run would plan tomorrow before today finished.

**Cron 403s** — `CRON_SECRET` isn't set, or doesn't match. Vercel sends it as
`Authorization: Bearer $CRON_SECRET`.
