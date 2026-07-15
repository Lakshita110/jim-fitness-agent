# CLAUDE.md — Jim

Jim is a personal training agent, now multi-tenant: any number of athletes can
sign up with email + password, each connecting their own Garmin account and
editing their own playbook. Plans are built by talking to the coach in chat,
which reasons about the next session within that athlete's knee/ankle
constraints using real Garmin (+ a read-only Notion habit/knee log) history.
Nightly housekeeping keeps that history fresh (syncs Garmin/Notion, reconciles
the day, sweeps stale one-off workouts) but does not write a plan itself —
there's no auto-draft while the athlete sleeps. Workouts reach the watch only
when the athlete presses a button.

Not a product. Real accounts, real isolation (`user_id`-scoped Postgres +
`kv`), but still one deployment, one Postgres, one operator.

## The shape of it

**One planning path (chat), one housekeeping cron, one hard guardrail in front of the watch.**

```
                    ┌── nightly cron ──┐
                    │  jobs/nightly.py │   sync + reconcile + cleanup only
                    └──────────────────┘   (writes history tables, no draft)
                              │
                              ▼ feeds
                    ┌── the chat ──────┐        ┌──────────────┐
   you ────────────▶│  coach.py        │───────▶│  validate ◀──┼── HARD
                    └──────────────────┘        └──────────────┘   guardrail
                              │
                              │ explicit push
                              ▼
                          Garmin ──▶ watch
```

Everything expensive is deterministic Python. The LLM is generative when
composing/editing a session in chat, turning `{state + playbook + goals}` into
a structured session, and conversational otherwise. It never decides whether
something is safe.

(One narrow exception, on the push path: naming an exercise Garmin's word-matcher
can't place. It's gated, batched, cached, and its answer is validated against
Garmin's real taxonomy — see `tools/exercise_match.py`.)

### The pieces

| Module | Job |
|---|---|
| `config.py` | Model tiers, tool-call caps, and the guardrail bounds. Behavioural constants live here so they're grep-able. |
| `schemas.py` | The typed contracts. `StructuredSession` is the currency of the whole system. |
| `db.py` | Postgres, idempotent migrations, and a `kv` store that holds most of the mutable state. |
| `tools/garmin.py` | Reads recovery/activities/per-set data; builds and schedules structured workouts, matching each movement to Garmin's exercise library. The verified JSON shape is hard-won — see `docs/garmin_strength.md` before touching it. |
| `tools/exercise_match.py` | The semantic fallback: names Garmin's word-matcher can't place, batched + cached + validated against the real taxonomy. |
| `tools/notion.py` | **Read-only.** Only the habits/knee log. Jim never writes to Notion. |
| `tools/history.py` | Deterministic features (volume, muscle balance, days-since-legs, pain trend) and the readiness read (acute:chronic workload + recovery → push/steady/ease/rest). |
| `tools/research.py` | Gated: curated corpus (pgvector) + Tavily. Only reachable when the off-heuristic fires. |
| `agent/validate.py` | The guardrail. Read the module docstring; the reasoning matters. Shared by chat's draft merge. |
| `playbook.py` | `load_playbook(user_id)`/`save_playbook` — the JSONB `playbooks` row per account; disk YAML is only the seed source now. |
| `auth.py` | Email + password signup/login, signed session cookies (`itsdangerous`), `_require_user`. |
| `crypto.py` | AES-GCM encrypt/decrypt for Garmin/Notion credentials at rest (`CREDENTIAL_ENCRYPTION_KEY`). |
| `coach.py` | The chat: conversation, lookups, draft merging, goals memory, pushing — all `user_id`-scoped. |
| `app.py` | FastAPI app + `/health`, `/api/cron/nightly`, static/manifest, `/login` — wires in the `web/` routers below. |
| `web/deps.py` | `_ready`/`_current_user`/`_require_user` — cross-cutting request helpers every route group imports the *module*, not the names, from (so tests have one patch point). |
| `web/auth_routes.py` | `/auth/signup`, `/auth/login`, `/auth/logout`. |
| `web/chat_routes.py` | The chat API + the `/chat` page. |
| `web/playbook_routes.py` | `/api/playbook`, `/api/garmin/workouts*` — the playbook editor and Garmin-workout import. |
| `web/garmin_routes.py` | `/settings/garmin*`, `/api/garmin/status` — Garmin account connect + MFA. |
| `web/templates.py` | The three inline HTML/CSS/JS page strings: `CHAT_PAGE`, `LOGIN_PAGE`, `GARMIN_PAGE`. No build step (see "Working here"). |

## Load-bearing decisions

These are the ones that will bite you if you don't know them.

**The guardrail splits hard from advisory.** Hard rules *reject a day*:
forbidden movements (knee/ankle), session length, Garmin's ~50-step cap, and
leg-day spacing. Balance across legs/push/pull/core/conditioning is **advice**
fed back to the model, never a rejection. There is deliberately **no weekly
volume cap** — a weekly budget checked per-day rejected any normal session and
made a full week impossible to build. An unbalanced week is suboptimal; a
silently dropped day is worse.

**Propose-only.** `AUTO_PUSH = False`. Chat writes a *draft*, never the watch.
Workouts are scheduled only by `coach.approve()` / `coach.push_day()` — i.e. a
button. There's no eval suite gating a flip to auto-push yet (the old M5
scaffold, `evals/run_evals.py`, tested the now-removed nightly auto-compose
path; a chat-turn eval would need a different shape, one scenario per
conversation, not per cron run).

**Nightly is housekeeping, not planning.** `jobs/nightly.py` syncs Garmin/Notion
into history tables, reconciles today's adherence, and sweeps stale one-off
Garmin adaptations — it never writes to the `draft` kv. The athlete gets a plan
only by asking the coach for one; there's no unsolicited overnight proposal to
step aside for.

**Drafts merge, they don't replace.** The model returns only the days it
changed; they're merged onto the plan by `for_date`. Then the *merged* plan is
validated as a whole — leg spacing only means anything when the days are seen
together. Fail once → revise → still failing → drop the day with a note.

**Every side effect is injected.** `CoachDeps` (coach) exists so the tests
never touch Postgres, Garmin, Notion, or an LLM. Keep it that way: if you add a
dependency, add it to the dataclass.

**A day is either a template pick or an adaptation — never both.** A base workout
already exists on Garmin with real loaded weights, so scheduling it by
`garmin_workout_id` preserves them; Jim hand-builds only when adapting. The model
cannot be trusted to keep these apart — it routinely echoes a template's ID
alongside steps it just edited — so `playbook.use_existing_workout()` decides in
code, and **steps win**: any day whose steps diverge from the template is built
fresh. Reading the ID first meant the athlete's edits were silently discarded and
stock Full Body A landed on the watch.

**Prescribe from reality.** Before setting a weight, the coach calls
`exercise_history("goblet squat")` and reads back actual sets × reps @ kg from
the watch, then progresses conservatively. Don't let it guess.

**Isolation is enforced in code and schema, not by physical separation.** One
Postgres, one deployment, every athlete's row carries `user_id` — `kv` as a
composite `(user_id, key)` primary key, every history table the same. A missed
`WHERE user_id = %s` or a closure that captures the wrong id is a silent
cross-account leak, not a crash, which is why `tests/test_multi_user_isolation.py`
exists and is treated as load-bearing, not a nice-to-have.

**Every movement must land on a real Garmin exercise.** Garmin's taxonomy is a
closed enum; a step it can't place arrives as a bare note — no exercise, no
animation, no set logging. So the whole library is vendored
(`data/garmin_exercises.json`, refreshed by `scripts/refresh_garmin_exercises.py`)
and matching is layered: hand-kept overrides for the PT movements Garmin genuinely
lacks → nearest name by word overlap → an LLM for what the words can't settle.
Words alone go wrong in both directions (nothing at all for `hip airplane`;
confidently `PLATE_RAISES` for a tibialis raise), so the model arbitrates anything
below `CONFIDENT_MATCH_SCORE` — but **its answer is validated against the library**,
because it will invent enums, and a described step beats a wrong one. The rules and
the mis-pushes behind them are in `docs/garmin_strength.md`.

## State

Postgres tables are history (`garmin_daily`, `garmin_activities`,
`exercise_sets`, `notion_daily_log`, `suggestions`, `outcomes`,
`research_corpus`), every one of them `user_id`-scoped. Live mutable state is
in the `kv` store, keyed by `(user_id, key)` — one physical table, isolated
per account by the composite primary key, not by a string-prefix convention:

| Key | What |
|---|---|
| `draft` | The working plan — up to 7 dated `StructuredSession`s |
| `goals` | Plain-text long-term goals, rewritten by the model on request |
| `pushed` | What's on the watch: per date, a title + content hash (that hash is how the UI badges a day *modified since push*) |
| `chat_history` | Last 30 messages |
| `state` | Day snapshot (garmin/notion/features/readiness), cached 1h |
| `exercise_map` | Movement name → the Garmin exercise it resolved to (nulls cached too, so a name costs one LLM call ever) |

Memory hierarchy, from most to least durable: `playbook/directives.md` (git,
you edit it) → `goals` (chat) → `draft` (chat-only) → history tables (kept
fresh by nightly housekeeping). The guardrail sits above all of it and nothing
can override it. See `docs/memory.md`.

## Working here

```bash
ruff check . && pytest          # 224 tests, all offline — no live APIs in CI
uvicorn jim.app:app --reload    # sign in/up at /login, chat at /chat
python -m jim.jobs.nightly      # nightly housekeeping (sync/reconcile/cleanup),
                                 # by hand — fans out over every nightly_enabled
                                 # user; python scripts/backfill_users.py seeds
                                 # the original athlete's account first
```

- **Tests are offline, always.** Recorded fixtures, injected fakes. A test that
  needs a network is a test that's wrong.
- **Migrations are additive and idempotent** (`src/jim/migrations/`). They ship
  inside the package and run on the request path — serverless has no reliable
  startup hook. Never edit a migration that's been applied; add `00N_*.sql`.
- **Cost discipline is a feature.** Nightly housekeeping makes zero LLM calls.
  Chat: 1 call/turn + ≤4 lookup rounds, state cached, history truncated, research
  gated. Before you hand the model a job, ask whether Python could do it.
- **The chat UI has no build step** — it's one inline HTML/CSS/JS string in
  `web/templates.py` (hence its `E501` exemption in `pyproject.toml`). After
  changing it, the `responsive-check` agent verifies it across widths.
- **Secrets never land in git.** `.env` locally; Vercel env vars in prod.

## Where to read next

- `docs/architecture.md` — the same picture with a full diagram and the flow in words
- `docs/chat.md` — how the chat behaves
- `docs/memory.md` — how to give Jim instructions
- `docs/garmin_strength.md` — the verified workout JSON, and each rule that cost a live 400
- `docs/notion_schema.md` — the habits-log property mapping
- `DEPLOY.md` — Vercel + Neon, and the serverless gotchas
- `HANDOFF.md` — local setup
- `PLAN.md` — the original design record (superseded in places; it says so)
