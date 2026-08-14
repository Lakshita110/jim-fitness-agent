# Jim's chat — API

**This document describes the `/chat/*` API backed by `coach.py`**, which is
still active but staged for removal once the Garmin MCP path
(`src/jim/mcp_server.py`, mounted at `/mcp`, operating instructions in
`skills/jim-coach/SKILL.md`) is verified end-to-end. In the MCP path an
MCP-capable client (e.g. Claude) talks to Garmin directly instead of through
this API — see `CLAUDE.md` for current status.

One conversation with your coach, exposed as a JSON API. A client iterates on
a plan for tomorrow or the week, keeps long-term goals in plain language, and
**nothing touches your watch until it calls `/chat/approve` or
`/chat/push-day`**.

No frontend ships in this repo — this doc describes the endpoints a UI (or
`curl`, or a test) drives. All `/chat/*` and `/settings/garmin/*` routes
require a session cookie from `/auth/login` or `/auth/signup`.

## Setup

1. Set `SESSION_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` in the environment
   (both long random strings). `OPENROUTER_API_KEY` must also be set — the
   conversation runs on the cheap tier (`MODEL_FAST`).
2. `POST /auth/signup` or `POST /auth/login` with `{"email", "password"}` —
   see `scripts/backfill_users.py` for creating the original athlete's
   account from existing env-var credentials. A successful login/signup sets
   a session cookie (~13 months), so later requests need no key or password.
3. Not connected to Garmin yet? `POST /settings/garmin/connect` with
   `{"garmin_email", "garmin_password"}` (and `POST /settings/garmin/mfa`
   with `{"mfa_code"}` if the account needs it). Jim stores the password
   encrypted at rest — Garmin has no OAuth, so there's no way around holding
   it — and re-authenticates silently with it if a cached session token
   expires.

## Endpoints

- `GET /chat/state` — current history, draft, goals, and push status for the
  signed-in user.
- `POST /chat/message` — `{"text", "scope_date"?}`. Send a message; optional
  `scope_date` (ISO date) scopes the turn to editing just that day.
- `POST /chat/plan-week` — `{}`. Kick off a full-week plan through the same
  conversational path (`coach.plan_week()` funnels through `converse()`).
- `POST /chat/approve` — `{}`. Push the whole current draft to Garmin.
- `POST /chat/push-day` — `{"date"}`. Push (or re-push) a single day.
- `POST /chat/clear` — `{}`. Clear chat history; draft and goals survive.
- `GET /api/garmin/status`, `POST /settings/garmin/connect`,
  `POST /settings/garmin/mfa` — Garmin account connection.
- `GET /api/playbook`, `POST /api/playbook` — read/write the JSON playbook
  (base workouts, PT routines, rotation, directives). Saves are
  all-or-nothing: a bad edit gets a 400 with the validation error, not a
  half-applied playbook. New accounts start from a generic empty seed, not
  anyone else's knee-specific content.

## How it behaves

- **It's one thread per account** — chat history, draft, goals, and playbook
  are isolated per user, never shared across accounts on the same deployment.
  `/chat/clear` starts the conversation fresh — the draft and goals survive a
  clear.
- **Nothing is planned until asked** — there's no overnight auto-draft. A
  message like "plan tomorrow" or "what should legs look like this week?"
  is what fills in the draft returned from `/chat/message`.
- **Iterate freely**: "swap Thursday to home PT", "make the week easier,
  knee's cranky", "plan the whole week around the 5k goal". Every draft is
  run through the same hard guardrail — forbidden movements, session length,
  Garmin's step cap, leg-day spacing. Days that break it get revised once,
  then dropped with a note; they are never pushed. There is **no weekly
  volume cap**: plan as many days as you want, as long as each day is sane.
  How the plan spreads across legs/push/pull/core/conditioning is *advice*
  fed back to Jim, not a rejection.
- **Edits merge, they don't replace**: Jim returns only the days he changed
  and they're merged onto the plan by date, so tweaking Tuesday leaves the
  rest of the week alone. Passing `scope_date` on `/chat/message` scopes the
  turn to that date.
- **Long-term goals**: a message like "my long-term goal is X" rewrites the
  goals block — stored durably, nothing scheduled. Goals are read by every
  chat turn, so they shape plans continuously.
- **Jim looks things up** (bounded to 4 lookup rounds per turn): your
  per-exercise performance history from the watch (actual sets × reps @ kg —
  checked before any weight is prescribed, progressed conservatively), your
  recent workouts + adherence, and research (curated corpus + web) for
  pain-driven substitutions with citations.
- **Push to Garmin**: only `/chat/approve` and `/chat/push-day` do it — the
  conversation itself never pushes. Push the whole draft, or one day at a
  time. Template days schedule your existing Garmin workout (weights
  preserved); adapted days are created then scheduled; a `rest` day clears
  the watch for that date. Re-pushing a day replaces the old one rather than
  duplicating it, and `push_status` in the response marks each day
  **pushed** or **modified** (edited since it went to the watch). Days
  planned in chat are marked `source='chat'`.

## Cost discipline

State (Garmin recovery, history features) is snapshotted once and cached for
an hour, so a chat turn is a single cheap LLM call; a validator rejection adds
at most one more.
