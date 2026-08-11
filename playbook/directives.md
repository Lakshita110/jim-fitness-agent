# Directives — standing instructions for Jim

<!--
This file holds standing rules, in plain English; it's loaded into Jim's
context on every nightly run AND every chat turn. Keep it short and
imperative. Jim treats these as rules, above its own judgement, below the
hard safety guardrail in code. Day-to-day input and long-term goals go
through the chat instead (docs/chat.md).
-->

## Weekly shape

- Three lifting days a week, rotating **Full Body A → B → C** (see
  `base_workouts.yaml`). Never two lifting days back-to-back.
- On non-lifting days, do a **PT routine** (`pt_home`/`pt_gym` in
  `base_workouts.yaml`) unless the day is a planned rest.
- At least one full rest day a week; add rest when recovery is poor.

## Lifting days

- Schedule the existing Garmin workout for the next letter in the rotation.
  Don't invent a new lifting session unless adapting for pain (below).
- Skip a lifting day and swap to PT or rest if any of: pain ≥ 4/10, readiness
  very low, or a leg session happened in the last 2 days.

## PT days — home vs gym

- If I said where I'll be (in chat), that wins (home → `pt_home`,
  gym → `pt_gym`).
- Otherwise default to **gym PT** on weekdays, **home PT** on weekends or when
  tomorrow's tasks/travel mean no gym.
- If you still can't tell, pick home PT — it needs the least equipment.
- Always keep the ★ priority exercise (ankle eversion) in; it's the driver for
  the lateral ankle pain.

## When I give day-specific input in chat

- Treat it as a strong preference: honor it unless it breaks a hard rule or
  the pain guardrail — then follow the rule and tell me why.
- If I give a time budget, fit the session to it (trim accessory work, keep
  the priority + iso-anchor moves).
- Nothing said = decide from recovery, pain, goals, and the rotation as usual.

## Pain & flare rules (from PT — do not override)

- **Next-morning test.** The verdict is tomorrow's pain, not today's. If
  yesterday's session left me worse, back off ~20% today.
- **2/10 ceiling.** Keep working pain at or below 2–3/10.
- **Flare mode** (pain elevated / knee flaring): keep knee bend to ~60°, drop
  every `skip_on_flare` exercise, prefer PT or easy Zone-2 cardio over lifting.

## Adapting a session

- Prefer scheduling a base template unchanged. Only hand-build a modified
  session when pain/recovery forces a substitution — and when you do, cite the
  PT protocol or a research snippet for the swap.
- Left knee is the reactive one; note left–right asymmetry when it shows up.
