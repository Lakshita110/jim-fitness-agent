# Jim — architecture

One cheap-LLM agent, two entry points (a nightly cron and a chat), a hard
deterministic guardrail in front of anything that reaches the watch, and
memory split by how durable it is.

```mermaid
flowchart TB
    subgraph you["You"]
        WATCH["Garmin watch"]
        PHONE["Phone — Jim's chat<br/>(add-to-home-screen page)"]
        NOTIONAPP["Notion<br/>(habit journal, tasks)"]
    end

    subgraph vercel["Vercel"]
        subgraph web["web service (FastAPI)"]
            CHATUI["/chat page + API<br/>CHAT_SECRET"]
            COACH["coach.py<br/>conversation engine<br/>MODEL_FAST via OpenRouter"]
        end
        CRON["nightly cron 21:00<br/>jobs/nightly.py"]
        subgraph pg["Postgres"]
            KV["kv: chat history ·<br/>working draft · goals ·<br/>state cache"]
            TABLES["garmin_daily · activities ·<br/>exercise_sets (reps+kg per set) ·<br/>notion_daily_log · suggestions ·<br/>outcomes · research_corpus"]
        end
    end

    subgraph loop["agent core (shared)"]
        STATE["state tools<br/>garmin.py · notion.py (read-only) ·<br/>history.py features"]
        HEUR["heuristics.py<br/>off? → research gate<br/>ambiguous? → tier escalation"]
        COMPOSE["compose.py<br/>the ONE generative step"]
        VALIDATE["validate.py — HARD GUARDRAIL<br/>forbidden moves · session length ·<br/>weekly volume · leg-day spacing<br/>(revise once, then fallback)"]
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

    CRON -->|"reconcile today →<br/>plan tomorrow"| STATE
    STATE --> HEUR --> COMPOSE --> VALIDATE
    PLAYBOOK --> COMPOSE
    KV -->|goals| COMPOSE
    VALIDATE -->|"proposal = draft"| KV

    PHONE <--> CHATUI <--> COACH
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

**21:00 nightly** (`jobs/nightly.py`) — reconcile today's plan vs. actuals →
sync state → *if you already chat-planned tomorrow, stop* → otherwise: read
state + playbook + goals → cheap-heuristic research gate → compose (one LLM
call, escalating to the quality tier only on ambiguous state) → guardrail →
the proposal lands as the **chat draft**. Nothing is pushed unattended
(AUTO_PUSH stays off until the M5 evals gate it).

**Any time, in chat** (`coach.py`) — one continuous conversation. Each turn:
state snapshot (cached 1h) + playbook + goals + current draft + last 30
messages → the model may make up to 4 lookups (exercise history, workout
history, research) → returns `{reply, draft?, goals?}`. Draft days pass the
same guardrail. Saying "my long-term goal is…" rewrites the goals block —
memory without scheduling. **Push to Garmin** schedules each draft day
(template days by ID with your loaded weights; adapted days created fresh)
and marks them `source='chat'` so the nightly run steps aside.

## Memory hierarchy

| Layer | Store | Written by | Horizon |
|---|---|---|---|
| directives.md | git | you | standing policy |
| goals | Postgres kv | chat | months |
| draft | Postgres kv | chat + nightly | this week |
| exercise_sets / suggestions / outcomes | Postgres | the system | history |

## Cost discipline

- Deterministic Python computes features; the LLM only composes and converses.
- Nightly: ≤2 LLM calls (compose + one revision), research gated by a
  heuristic, quality tier only on ambiguous state, hard tool-call cap.
- Chat: 1 LLM call per turn + ≤4 lookups, state cached, history truncated.
- Guardrail and fallback are code, not model.
