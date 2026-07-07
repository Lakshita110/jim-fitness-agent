# Memory & instructions

Vesper has three memory layers, separated by how often each changes and who
edits it. This is the answer to "how do I give the agent instructions."

## 1. Playbook — durable knowledge you edit (files)

`playbook/` holds what the agent should know before reasoning:

| File | What it is | Source of truth |
|---|---|---|
| `base_workouts.yaml` | The A/B/C strength rotation | the Garmin workouts (IDs referenced) |
| `pt_routines.yaml` | PT for non-lifting days: `pt_home` + `pt_gym` | these files (gym reuses a Garmin workout) |
| `directives.md` | **Standing instructions, in plain English** | you |

The whole playbook is rendered into the agent's context every night
(`playbook.py:load_playbook().to_prompt()`). **To instruct the agent, edit
`directives.md`** — e.g. "no lifting until the flare settles", "prefer home PT
this week", "add a fourth lifting day". The change takes effect on the next run;
no code, no redeploy. Directives sit above the agent's own judgement and below
the hard-coded safety guardrail (`agent/validate.py`), which can't be overridden
from a file.

Base workouts carry `garmin_workout_id`, so on a lifting day the agent selects a
template and the loop schedules **that existing Garmin workout** (keeping your
loaded weights). It only hand-builds a new workout when adapting for pain.

Editing rules of thumb:
- Change reps/exercises in a base workout on Garmin *and* mirror it in the YAML
  (Garmin is what lands on the watch; the YAML is what the agent reasons about).
- `pt_home` has no Garmin workout yet — it's propose-only until one is created.
- Tags the agent honors: `priority` (never drop — the ★ ankle eversion),
  `skip_on_flare`, `iso_anchor`; `equipment` drives the home-vs-gym choice.

## 2. Episodic memory — what actually happened (Postgres)

`suggestions` + `outcomes` (PLAN.md §6). Each night's proposal is recorded; the
morning reconcile job matches Garmin actuals and writes adherence. This is what
the agent *learns from* across days — not something you edit by hand.

## 3. Garmin — the exercise library

Your structured workouts on Garmin Connect are the canonical exercise
definitions. The playbook points at them by ID rather than duplicating the
weight/exercise data, so "schedule Full Body B" reuses the real thing.

## Quick "how do I…"

- **Tell the agent a standing rule** → edit `playbook/directives.md`.
- **Change a base workout** → edit it on Garmin, mirror reps in
  `base_workouts.yaml`.
- **Add/adjust a PT routine** → edit `pt_routines.yaml`.
- **One-off ("skip tomorrow")** → add a line to `directives.md` and remove it
  after; there's no per-day override channel yet (could be a Notion field later).
