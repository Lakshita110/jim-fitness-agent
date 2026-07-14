# Jim's chat

One conversation with your coach. You iterate on a plan for tomorrow or the
week, keep long-term goals in plain language, and **nothing touches your watch
until you hit Push to Garmin**.

## Setup

1. Set `SESSION_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` in the environment
   (both long random strings). `OPENROUTER_API_KEY` must also be set — the
   conversation runs on the cheap tier (`MODEL_FAST`).
2. Open `https://<your-app>.vercel.app/login` and sign in (or create an
   account — see `scripts/backfill_users.py` for creating the original
   athlete's account from existing env-var credentials). A successful
   login/signup sets a session cookie (~13 months), so later visits go
   straight to `/chat` with no key or password in the URL.
3. Not connected to Garmin yet? Open **Settings → Garmin** (`/settings/garmin`)
   and enter your Garmin email/password (MFA-code form if your account needs
   it). Jim stores the password encrypted at rest — Garmin has no OAuth, so
   there's no way around holding it — and re-authenticates silently with it if
   a cached session token expires.
4. On your phone: **Add to Home Screen** — it opens full-screen like an app,
   authenticated by that cookie.

## How it behaves

- **It's one thread per account** — your chat history, draft, goals, and
  playbook are yours alone, isolated from any other athlete on the same
  deployment. "clear chat" starts fresh — the draft and your goals survive a
  clear.
- **The page shows** three stat cards (readiness verdict, next session, latest
  pain read) and a persistent Plan panel — right column on desktop, bottom sheet
  on mobile. Replies render light markdown, so a weekly schedule comes back as a
  formatted list rather than raw asterisks.
- **The nightly run's proposal appears as the WORKING PLAN card** when you
  open the chat. Tweak it in conversation or just push it.
- **Iterate freely**: "swap Thursday to home PT", "make the week easier,
  knee's cranky", "plan the whole week around the 5k goal". Every draft is
  run through the same hard guardrail as the nightly agent — forbidden
  movements, session length, Garmin's step cap, leg-day spacing. Days that
  break it get revised once, then dropped with a note; they are never pushed.
  There is **no weekly volume cap**: plan as many days as you want, as long as
  each day is sane. How the plan spreads across legs/push/pull/core/
  conditioning is *advice* fed back to Jim, not a rejection.
- **Edits merge, they don't replace**: Jim returns only the days he changed and
  they're merged onto the plan by date, so tweaking Tuesday leaves the rest of
  the week alone. Editing a single day in the Plan panel scopes the turn to
  that date.
- **Long-term goals**: say "my long-term goal is X" and Jim rewrites your
  goals block — stored durably, nothing scheduled. Goals are read by every
  chat turn *and* every nightly run, so they shape plans continuously.
- **Playbook panel**: a "Playbook" link next to the plan opens a JSON textarea
  (`GET`/`POST /api/playbook`) — your base workouts, PT routines, rotation, and
  standing directives, editable directly. Saves are all-or-nothing: a bad edit
  gets a 400 with the validation error, not a half-applied playbook. New
  accounts start from a generic empty seed, not anyone else's knee-specific
  content.
- **Jim looks things up** (bounded to 4 lookup rounds per turn): your
  per-exercise performance history from the watch (actual sets × reps @ kg —
  checked before any weight is prescribed, progressed conservatively), your
  recent workouts + adherence, and research (curated corpus + web) for
  pain-driven substitutions with citations.
- **Push to Garmin**: a button, always — asking Jim in words won't do it.
  Push the whole draft, or one day at a time from the Plan panel. Template days
  schedule your existing Garmin workout (weights preserved); adapted days are
  created then scheduled; a `rest` day clears the watch for that date. Re-pushing
  a day replaces the old one rather than duplicating it, and each day badges as
  **pushed** or **modified** (edited since it went to the watch). Days you plan
  in chat are marked `source='chat'` and the nightly run **steps aside** for them.

## Notion's role

Read-only, and only the habits/knee log. Jim never writes to Notion, and does
not read Notion tasks — scheduling context comes from Garmin. The `tasks `,
`training check-in`, and `training proposals` databases are dormant — delete
them in Notion whenever you like.

## Cost discipline

State (Garmin recovery, history features, Notion log) is snapshotted once and
cached for an hour, so a chat turn is a single cheap LLM call; a validator
rejection adds at most one more.
