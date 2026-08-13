# CLAUDE.md — Jim

Jim is a personal training agent, multi-tenant: any number of athletes sign up
with email + password, each connecting their own Garmin account and editing
their own playbook. Plans are built by talking to a coach in chat, which
reasons about the next session within that athlete's knee/ankle constraints
using real Garmin history. A nightly cron keeps that history fresh (sync,
reconcile, cleanup) but never writes a plan itself; workouts reach the watch
only when the athlete presses a button.
Not a product — real accounts and real per-user isolation, but one deployment,
one Postgres, one operator.
Backend-only: this is a pure JSON API with no HTML/frontend routes. The old
inline HTML/CSS/JS pages have been scrapped; a new UI will be built separately
against the API described below.

**In transition:** Claude is becoming the reasoning engine, talking to Garmin
directly through the MCP server at `/mcp` (`mcp_server.py`) instead of through
`coach.py`'s own conversation loop. `coach.py`, `playbook.py`, and
`agent/validate.py` are still present and still power the old `/chat/*` +
`/api/playbook` routes, but are staged for removal once the MCP path is
verified end-to-end (see the approved plan for this work). The one piece of
Jim-side state the MCP path still needs is the small `constraints` table
(knee/ankle limits, standing rules, goals) — everything the playbook's
template library used to hold now lives in Garmin's own workout library.

## Module table

| Path | Purpose | Status |
|---|---|---|
| `api/index.py`, `vercel.json` | Serverless entrypoint + deploy config | active |
| `src/jim/app.py` | FastAPI app, `/health`, `/api/cron/nightly`, mounts the MCP app at `/mcp`, wires in `web/` routers | active |
| `src/jim/mcp_server.py` | Garmin MCP — read history/readiness/calendar/workout library, write create/schedule/unschedule, get/set constraints. Bearer-token auth, re-resolved per call (see its docstring for why) | active, new |
| `skills/jim-coach/SKILL.md` | Operating instructions for Claude when it's the one calling the MCP tools — constraints-first, data-grounded recommendations, never write without an explicit ask, `set_constraints` is a full replace. This is the safety layer now that there's no code guardrail | active, new |
| `src/jim/web/{auth,chat,garmin,playbook,constraints}_routes.py`, `deps.py` | Pure JSON API routes (no HTML). `auth_routes` now also returns a bearer token on login/signup for non-browser clients (the MCP server) | active |
| `src/jim/coach.py` | Chat: conversation, lookups, draft merge, goals memory, push, `plan_week()` | active |
| `src/jim/schemas.py` | Typed contracts, incl. `StructuredSession` | active |
| `src/jim/playbook.py` | Load/save the per-user playbook (Postgres JSONB); disk YAML is seed only | active |
| `src/jim/config.py`, `db.py`, `auth.py`, `crypto.py` | Settings, Postgres/`kv`, auth, at-rest credential encryption | active |
| `src/jim/agent/validate.py` | The hard guardrail — read the docstring before editing | active |
| `src/jim/tools/garmin.py` | Garmin reads/writes + workout scheduling | active |
| `src/jim/tools/exercise_match.py` | LLM fallback for unmatched exercise names, validated against the vendored taxonomy | active |
| `src/jim/tools/history.py` | Deterministic features + readiness read | active |
| `src/jim/tools/research.py` | Gated corpus/Tavily research | active |
| `src/jim/tools/memory.py` | Suggestion/outcome recording (`record_suggestion`, `record_outcome`); used by `jobs/reconcile.py` | active |
| `src/jim/jobs/nightly.py` | Sync + reconcile + cleanup cron; never drafts a plan | active |
| `src/jim/jobs/reconcile.py` | Matches Garmin actuals to stored suggestions | active |
| `src/jim/migrations/001–011_*.sql` | Additive, idempotent, never edited after applied | active |
| `src/jim/data/garmin_exercises.json` | Vendored Garmin exercise taxonomy | active |
| `playbook/{base_workouts.yaml,directives.md}` | The real athlete's committed content — one flat workout library (strength + PT) plus rotation order, real `garmin_workout_id`s | active |
| `playbook/defaults/*` | Intentionally empty/generic seed for new signups | active |
| `data/corpus/*` | Research corpus source + template | needs-review — not traced to `research.py` ingestion path this session |
| `scripts/backfill_users.py` | One-off: creates the original athlete's user row, seeds their playbook, backfills `user_id` onto pre-multi-tenant rows. Not idempotent by design | active (one-off, already run) |
| `scripts/backfill.py` | Repeatable ~90-day Garmin history backfill for an existing user. Idempotent | active |
| `scripts/exercise_map.py`, `garmin_login.py`, `make_icon.py`, `refresh_garmin_exercises.py`, `seed_corpus.py` | Ops/dev utilities | needs-review — not individually verified this session |
| `scripts/m1_roundtrip.py` | Standalone dev CLI, named for an early single-user milestone; referenced only in `README.md` and a comment in `auth.py`, never imported | stale — likely safe to remove, confirm before deleting |
| `tests/*` | Offline, fixture/fake-driven, one file per module + `test_multi_user_isolation.py` (load-bearing) | active |
| `docs/*.md`, `README.md`, `HANDOFF.md` | Architecture/chat/memory/deploy docs | needs-review — not re-verified against current code this session |

## Architecture rules (from the code, not assumed)

- **One planning path.** Only `coach.py` (chat) writes drafts; `jobs/nightly.py` only syncs/reconciles/cleans up. The "Plan my week" button is not a second path — `plan_week()` funnels through `converse()`, supplying an instruction and then re-asserting days already on the watch.
- **Propose-only.** Nothing pushes to Garmin except an explicit `coach.approve()`/`push_day()` call from a user action.
- **The rotation is followed, not guessed.** `tools/history.last_rotation_key()` reads the last pushed `template_key` out of `suggestions.plan`, and `Playbook.rotation_from()` turns it into the continuing order, which the prompt's `# ROTATION` block hands the model. The model still chooses; it just isn't inferring the sequence from workout titles.
- **Playbook writes are explicit-request-only**, and there are exactly three: `promote_garmin_workout` (keep a pushed one-off), `save_workout_template` (upsert a template), `set_rotation` (replace the order). A one-off session or a single-day tweak writes none of them.
- **Guardrail is the single safety authority.** `agent/validate.py` hard-rejects unsafe days; balance is advisory only. No component bypasses it.
- **User isolation is structural, not conventional.** Every history table and `kv` row carries `user_id` in a composite key — see `tests/test_multi_user_isolation.py`.
- **Side effects are injected**, not imported ad hoc — `CoachDeps` keeps tests offline.
- **Migrations are append-only** — `00N_*.sql`, never edited post-apply.

## Playbook defaults split & backfill relationship

`playbook/*` (top level) is the original athlete's real, committed content —
actual `garmin_workout_id`s and knee-specific programming. `playbook/defaults/*`
is a deliberately empty/generic seed so a brand-new signup never inherits
someone else's Garmin IDs or PT protocol. `playbook.py` loads the top-level
files via `_load_playbook_from_disk(PLAYBOOK_DIR)` and the defaults via
`_load_default_playbook()` → `DEFAULT_PLAYBOOK_DIR`; both are live, not a
stale duplicate.

The two backfill scripts are sequential, not redundant: `backfill_users.py`
runs once to migrate the original athlete into the multi-tenant schema
(creates their user row, seeds their playbook from disk, backfills `user_id`
onto legacy NULL rows); `backfill.py` runs repeatably afterward to pull
rolling Garmin history for an existing user.

## Commands

```bash
ruff check . && pytest          # offline test suite
uvicorn jim.app:app --reload    # JSON API at /auth, /chat, /api/*, MCP at /mcp
python -m jim.jobs.nightly      # nightly housekeeping, by hand
```

**Unresolved:** whether `data/corpus/*` and the five untraced `scripts/*`
utilities are still exercised by any current path — flagged above rather than
guessed at.
