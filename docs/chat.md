# Chat interface

Talking to Jim replaces the morning cron entirely: a message IS a check-in,
and it triggers an immediate re-plan with the reply being tomorrow's (or
today's) proposal.

## Built-in web chat (no third-party account)

The web service hosts a private chat page:

1. Set `CHAT_SECRET` in the environment (long random string; chat is disabled
   without it).
2. Open `https://<your-service>.onrender.com/chat?key=<CHAT_SECRET>`.
3. On your phone: **Add to Home Screen** — it opens full-screen and behaves
   like a messaging app.

Examples of messages:

- "left knee sore, keep it light, home only, 30 min"
- "tomorrow: want upper body, gym"
- "today easy spin, slept terribly"

## Which day does a message apply to?

- Before **15:00 local** (`CHAT_TODAY_CUTOFF_HOUR`): re-plans **today** — the
  morning-coffee case.
- After 15:00: check-in for **tomorrow** (the plan is built immediately; the
  nightly cron won't override it unless state changed).
- A leading word **"today"** or **"tomorrow"** always wins.

## How it works

`POST /chat/message` → `chat.handle_chat_message` → builds a
`CheckIn(note=<your message>)` → `run_agent(plan_for=<target>, checkin=...)` —
the same bounded loop as the nightly run: guardrail, playbook, research gating
all apply. The model reads your note verbatim, so no rigid format is needed.

## Other transports (not wired, by choice)

`handle_chat_message` is transport-agnostic; any of these is a thin webhook
route in `app.py` away:

- **WhatsApp** — via Twilio (sandbox re-join every 72h — annoying for personal
  use) or Meta's Cloud API directly (needs a Meta dev app + business phone
  number; most setup of all options).
- **SMS** — Twilio number (~$1/month + per-message); works without any app.
- **Discord/Slack bot** — free and quick if you already live there.
- **iMessage** — no public API; not feasible.

## Nightly + chat = the whole system

- **One cron** (21:00): reconcile today's adherence, sync data, plan tomorrow.
- **Chat, anytime**: adjust today or tomorrow on demand.
- The Notion `training check-in` DB still works as a structured alternative —
  read at the nightly run. The optional morning job
  (`python -m jim.jobs.reconcile`) exists only for Notion-morning-check-in
  users; with chat, don't schedule it.
