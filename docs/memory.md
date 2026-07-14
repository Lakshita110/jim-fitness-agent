# Memory & instructions

Jim has four memory layers, ordered by how durable they are and who writes
them. This is the answer to "how do I give the agent instructions."

| Layer | Lives in | Written by | Horizon |
|---|---|---|---|
| Playbook directives | git (`playbook/directives.md`) | you, by hand | standing policy |
| Long-term goals | Postgres kv (`goals`) | the chat ("my goal is…") | months |
| Working draft | Postgres kv (`draft`) | chat + nightly run | this week |
| What's on the watch | Postgres kv (`pushed`) | the push buttons | until re-pushed |
| Suggestions/outcomes | Postgres tables | the system | history |

(Two more kv keys are plumbing rather than memory: `chat_history`, the last 30
messages, and `state`, the hourly-cached day snapshot.)

## 1. Playbook — durable knowledge you edit (files)

`playbook/` holds what Jim knows before reasoning:

| File | What it is | Source of truth |
|---|---|---|
| `base_workouts.yaml` | The A/B/C strength rotation | the Garmin workouts (IDs referenced) |
| `pt_routines.yaml` | PT for non-lifting days: `pt_home` + `pt_gym` | these files (both exist on Garmin) |
| `directives.md` | **Standing rules, in plain English** | you |

The whole playbook is rendered into context on every nightly run and chat
turn. Directives sit above the model's judgement and below the hard-coded
safety guardrail (`agent/validate.py` — forbidden movements, session length,
Garmin's step cap, leg-day spacing), which nothing can override.

Base workouts carry `garmin_workout_id`, so scheduling a template day pushes
**that existing Garmin workout** (loaded weights preserved). Jim only builds a
new workout when adapting for pain — and the verified exercise taxonomy in
`tools/garmin.py` makes adapted steps render as real exercises on-watch.

## 2. Long-term goals — direction, in your own words (chat-written)

Tell Jim a goal in chat ("run a 5k by spring — knee health first") and it
rewrites the plain-text goals block in the kv store. Nothing gets scheduled;
the block is folded into every plan afterwards (progressions, deloads,
milestones). Ask Jim "what are my goals?" or change them the same way.

## 3. Working draft — this week (chat + nightly)

The current plan-in-progress: written by the nightly run (its proposal) and by
chat iteration; pushed to Garmin only on explicit approve. Days approved in
chat are recorded `source='chat'` and the nightly run skips them.

A companion `pushed` key remembers what actually reached the watch — per date, a
title, timestamp, and a content hash of the session. Comparing that hash to the
current draft is how the UI knows a day was *edited since it was pushed*, and
re-pushing unschedules the old workout first so the watch never duplicates.

## 4. Episodic memory — what actually happened (Postgres)

`suggestions` + `outcomes`: each proposal is recorded; the nightly job
reconciles the day's Garmin actuals against it (adherence).
`exercise_sets`: every ACTIVE set from logged strength sessions (exercise,
reps, kg — synced nightly, backfillable). This is what Jim *learns from*: in
chat it looks up `exercise_history("goblet squat")` before prescribing a
weight and progresses from what you actually lifted.

## Notion: read source only

Jim reads the habits/knee log (pain, PT adherence, habits) — nothing else. It
never writes to Notion, and does not read Notion tasks (Garmin covers
scheduling).

## Quick "how do I…"

- **Standing rule** → edit `playbook/directives.md`.
- **Long-term goal** → tell Jim in chat.
- **This day/week** (pain, focus, home/gym, time) → tell Jim in chat, then
  Push to Garmin.
- **Change a base workout** → edit it on Garmin, mirror reps in
  `base_workouts.yaml`.
