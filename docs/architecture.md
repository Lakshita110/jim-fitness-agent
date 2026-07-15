# Jim — architecture

One cheap-LLM agent (the chat), a nightly cron that does housekeeping only —
no auto-drafted plan — a hard deterministic guardrail in front of anything
that reaches the watch, and memory split by how durable it is.

```mermaid
flowchart TB
    subgraph you["You"]
        WATCH["Garmin watch"]
        PHONE["Phone — Jim's chat<br/>(add-to-home-screen page)"]
        NOTIONAPP["Notion<br/>(habit journal, tasks)"]
    end

    subgraph vercel["Vercel"]
        subgraph web["web service (FastAPI)"]
            CHATUI["/chat page + API + PWA<br/>/login email+password → session cookie"]
            SETTINGS["/settings/garmin, /api/playbook<br/>Garmin connect + playbook editor"]
            COACH["coach.py<br/>conversation engine, per user_id<br/>MODEL_FAST via OpenRouter"]
        end
        CRON["nightly cron 20:00 UTC<br/>/api/cron/nightly → run_nightly()<br/>housekeeping only, fans out over<br/>every nightly_enabled user"]
        subgraph pg["Postgres"]
            USERS["users · user_credentials (Garmin/Notion,<br/>AES-GCM encrypted) · playbooks"]
            KV["kv (user_id, key): chat history ·<br/>working draft · goals · pushed map · state cache"]
            TABLES["garmin_daily · activities ·<br/>exercise_sets (reps+kg per set) ·<br/>notion_daily_log · suggestions ·<br/>outcomes · research_corpus<br/>— all user_id-scoped"]
        end
    end

    subgraph loop["shared reasoning tools"]
        STATE["state tools<br/>garmin.py · notion.py (read-only) ·<br/>history.py features + readiness"]
        VALIDATE["validate.py<br/>HARD (rejects): forbidden moves ·<br/>session length · step cap · leg spacing<br/>ADVISORY: balance notes<br/>(revise once, then fallback)"]
        PLAYBOOK["playbook/ (git)<br/>A/B/C base workouts ·<br/>PT home+gym · directives.md"]
    end

    subgraph lookups["coach lookups (bounded, ≤4/turn)"]
        EXH["exercise_history<br/>'goblet squat → 3x12 @ 16kg'"]
        WKH["workout_history<br/>recent sessions + adherence"]
        RESEARCH["research<br/>curated corpus (pgvector) + Tavily"]
    end

    GARMINAPI["Garmin Connect API<br/>(token auth)"]

    WATCH -->|"activities, HRV, sleep,<br/>per-set reps & weights"| GARMINAPI
    GARMINAPI -->|"nightly sync + backfill"| TABLES
    NOTIONAPP -->|"READ ONLY"| STATE

    CRON -->|"sync Garmin/Notion"| TABLES
    CRON -->|"reconcile + sweep<br/>stale adaptations"| KV
    STATE --> COACH
    PLAYBOOK --> COACH
    VALIDATE -->|"validated draft"| KV

    PHONE <--> CHATUI <--> COACH
    PHONE <--> SETTINGS -->|"encrypted creds ·<br/>playbook JSON"| USERS
    COACH <--> KV
    COACH -->|"draft days"| VALIDATE
    COACH <--> EXH & WKH & RESEARCH
    EXH --> TABLES
    WKH --> TABLES

    CHATUI -->|"Push to Garmin<br/>(explicit approve)"| GARMINAPI
    GARMINAPI -->|"scheduled workout<br/>syncs to watch"| WATCH
```

## The flow, in words

**Around the clock** — every strength session you log flows back: the nightly
sync stores the activity *and its per-set data* (`exercise_sets`: category,
exercise, reps, kg). That's the progression memory: when you ask Jim to "bump
goblet squats," it calls `exercise_history("goblet squat")`, sees
`2026-07-05: 3x12 @ 16kg`, and prescribes conservatively from reality.

**Nightly** (`jobs/nightly.py`, Vercel Cron at 20:00 UTC — deliberately after
the training day) — housekeeping only, no plan is written here. `run_nightly()`
selects every `users` row with `nightly_enabled = true` and runs the per-user
pipeline for each in turn: sync today's Garmin + Notion into Postgres (that
user's own credentials, that user's own `users.timezone` for "today") → sweep
stale one-off Garmin adaptations whose day has passed (built by chat, never
promoted into the playbook) → reconcile today's plan vs. actuals for the
adherence signal. One user's failure (expired Garmin creds, Notion down, a
Garmin hiccup during cleanup) is caught and logged at the per-user boundary —
it doesn't stop the rest of the run. The whole run must finish inside the
function's `maxDuration`, so it returns `elapsed_sec` alongside a per-user
result map.

**Any time, in chat** (`coach.py`) — one continuous conversation. Each turn:
state snapshot (cached 1h, each source degrading independently) + playbook +
goals + current draft + balance notes + last 30 messages → the model may make
up to 4 lookups (exercise history, workout history, research) → returns
`{reply, draft?, goals?}`. A returned draft is **merged by date** onto the
existing plan, so a single-day edit can't silently drop the rest of the week;
the merged plan is then validated as a whole (leg spacing only means something
when the days are seen together), revised once, and any day still failing is
dropped with a note. Saying "my long-term goal is…" rewrites the goals block —
memory without scheduling.

**Pushing** — the draft reaches the watch only on an explicit button: **Push to
Garmin** (whole draft, `coach.approve`) or a single day (`coach.push_day`).
Template days schedule the existing Garmin workout by ID (loaded weights
preserved); adapted days are created fresh. Re-pushing a day unschedules the
previous one first, so the watch never ends up with duplicates. Pushed days are
recorded `source='chat'` and tracked in the `pushed` kv map with a content
hash, which is what lets the UI badge a day as *pushed* or *modified since
push*.

## Multi-tenant data model

One deployment, one Postgres, any number of `users` rows — isolation is
enforced by `user_id`, not by separate databases. `users` holds login +
`timezone` + `nightly_enabled`; `user_credentials` holds each account's Garmin
email/password and Notion token, AES-GCM encrypted at rest
(`crypto.py`, key in `CREDENTIAL_ENCRYPTION_KEY`, never in the DB); `playbooks`
holds one JSONB row per account (base workouts, PT routines, rotation,
directives — edited via `/api/playbook`, seeded generic at signup). Every
history table (`kv`, `garmin_daily`, `garmin_activities`, `exercise_sets`,
`notion_daily_log`, `suggestions`, `outcomes`) carries `user_id` as part of its
primary key. `tools/garmin.py`/`tools/notion.py` keep a per-`user_id` client
cache (a plain dict, since each serverless instance is single-process); every
`Toolbox`/`CoachDeps` lambda closes over the `user_id` it was built for.

## Memory hierarchy

| Layer | Store | Written by | Horizon |
|---|---|---|---|
| directives.md | git | you | standing policy |
| goals | Postgres kv | chat | months |
| draft | Postgres kv | chat | this week |
| pushed | Postgres kv | the push buttons | until re-pushed |
| exercise_sets / suggestions / outcomes | Postgres | the system | history |

## Cost discipline

- Deterministic Python computes features; the LLM only composes and converses.
- Nightly housekeeping (sync/reconcile/cleanup) makes zero LLM calls.
- Chat: 1 LLM call per turn + ≤4 lookup rounds, state cached for an hour,
  history truncated to the last 30 messages, research gated by a heuristic.
- Guardrail, balance maths, and fallback are code, not model.
