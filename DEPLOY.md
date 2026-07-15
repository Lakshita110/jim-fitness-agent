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
| `SESSION_SECRET` | random string from step 1 |
| `CREDENTIAL_ENCRYPTION_KEY` | another random 32-byte base64 key from step 1 |
| `CRON_SECRET` | the other random string from step 1 |
| `GARMIN_TOKENS` | the session blob from step 1 |
| `GARMIN_EMAIL` | your Garmin email |
| `NOTION_TOKEN` | from `.env` |
| `NOTION_KNEE_LOG_DB_ID` | `b872f62a28604573980e983be6fd3143` |
| `OPENROUTER_API_KEY` | from `.env` |
| `TAVILY_API_KEY` | from `.env` |
| `APP_TIMEZONE` | `Europe/Berlin` |

`DATABASE_URL` is already there from step 2.

`GARMIN_TOKENS`/`GARMIN_EMAIL` only bootstrap the *original* athlete's account
(consumed by `scripts/backfill_users.py`, below) — every other account
connects Garmin itself through `/settings/garmin` in the browser, and its
credentials live encrypted in `user_credentials`, not in an env var.

## 5. Deploy and check

```bash
curl https://<your-app>.vercel.app/health          # {"status":"ok"}
```

The first chat request applies the migrations automatically.

**Deployment ordering (existing single-user data only):** if this deploy has
prior single-user data (`garmin_daily`, `kv`, etc. with no `user_id` yet),
migration `008_user_pks.sql` — which promotes those tables to composite
`(user_id, …)` primary keys — will fail and log a *warning*, not crash the
request, until `scripts/backfill_users.py` has been run against production to
give every existing row a `user_id`. This is the same downgrade-to-warning
behavior `db.py`'s `migrate()` already has for a missing `pgvector` extension.
Run the backfill once, against the Neon `DATABASE_URL`, before or after this
deploy — `008` applies cleanly and permanently on the next migration pass once
it has:

```bash
DATABASE_URL="postgres://…neon…" python scripts/backfill_users.py you@example.com
```

A brand-new deployment with no pre-existing data has nothing to backfill —
`008` applies immediately since there are no NULL `user_id` rows to violate it.

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

Open the login page **on your phone**:

```
https://<your-app>.vercel.app/login
```

Sign in (or sign up — see `scripts/backfill_users.py` for creating the
original athlete's account from the credentials already in Vercel's env
vars), then install it — it's a real PWA, so it gets its own icon and opens
fullscreen with no browser chrome:

- **iOS / Safari** — Share → **Add to Home Screen**
- **Android / Chrome** — ⋮ → **Install app**

The installed app launches straight into the chat. You only ever log in once:
a successful sign-in sets an httpOnly session cookie (~13 months) and the
manifest's `start_url` is the bare `/chat`, authenticated by that cookie. No
secret is baked into the manifest or the icons, which is why the browser can
fetch both during install.

> **Your password is the only thing protecting your chat.** Anyone who signs in
> can talk to Jim and push workouts to your watch. To invalidate every session
> at once (e.g. after rotating secrets): change `SESSION_SECRET` in Vercel and
> redeploy — every existing session cookie is signed with the old key, so they
> all stop verifying and each device has to sign in again.

---

## ⚠️ The one thing to watch: the nightly timeout

`maxDuration` is **60s** (the Hobby ceiling). The nightly run is housekeeping
only (no LLM call), but it still does real work per user — Garmin sync
(activities, sleep, HRV, per-activity exercise sets), stale-adaptation cleanup,
and reconcile. **If it exceeds 60s it is killed mid-run**, and it'll happen
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

**"No pending Garmin login (or it expired) — start again"** on the
`/settings/garmin/mfa` step — known limitation, not a security issue. The
pending-MFA state (`_pending_garmin_logins` in `web/garmin_routes.py`) lives in a
process-local dict, and Vercel serverless gives no guarantee that the request
carrying your MFA code lands on the same warm instance that issued the
challenge. It's usually transient — retry `/settings/garmin/connect` from the
top. A user who hits it repeatedly can fall back to
`python scripts/garmin_login.py` run locally (interactive, no serverless
instance-affinity problem) to mint a token blob, though the self-service
`/settings/garmin` flow is meant to make that unnecessary for most people.
