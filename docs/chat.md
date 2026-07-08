# Jim's chat

One conversation with your coach. You iterate on a plan for tomorrow or the
week, keep long-term goals in plain language, and **nothing touches your watch
until you hit Push to Garmin**.

## Setup

1. Set `CHAT_SECRET` in the environment (long random string; chat is disabled
   without it). `OPENROUTER_API_KEY` must also be set — the conversation runs
   on the cheap tier (`MODEL_FAST`).
2. Open `https://<your-service>.onrender.com/chat?key=<CHAT_SECRET>`.
3. On your phone: **Add to Home Screen** — it opens full-screen like an app.

## How it behaves

- **It's one thread** (you're the only user). "clear chat" starts fresh —
  the draft and your goals survive a clear.
- **The nightly run's proposal appears as the WORKING PLAN card** when you
  open the chat. Tweak it in conversation or just push it.
- **Iterate freely**: "swap Thursday to home PT", "make the week easier,
  knee's cranky", "plan the whole week around the 5k goal". Every draft is
  run through the same hard guardrail as the nightly agent (forbidden
  movements, session length, weekly volume, leg-day spacing) — days that
  break it get revised or dropped, never pushed.
- **Long-term goals**: say "my long-term goal is X" and Jim rewrites your
  goals block — stored durably, nothing scheduled. Goals are read by every
  chat turn *and* every nightly run, so they shape plans continuously.
- **Jim looks things up** (bounded to 4 lookups per turn): your per-exercise
  performance history from the watch (actual sets × reps @ kg — checked
  before any weight is prescribed, progressed conservatively), your recent
  workouts + adherence, and research (curated corpus + web) for pain-driven
  substitutions with citations.
- **Push to Garmin**: the button (or asking Jim won't do it — the button is
  the approval) schedules each day of the draft. Template days schedule your
  existing Garmin workout (weights preserved); adapted days are created then
  scheduled. Days you plan in chat are marked `source='chat'` and the nightly
  run **steps aside** for them.

## Notion's role

Read-only. Jim reads the habits/knee log and tasks as context; it never
writes to Notion. The earlier `training check-in` and `training proposals`
databases are dormant — delete them in Notion whenever you like.

## Cost discipline

State (Garmin recovery, history features, Notion log) is snapshotted once and
cached for an hour, so a chat turn is a single cheap LLM call; a validator
rejection adds at most one more.
