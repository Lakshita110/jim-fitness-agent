# CLAUDE.md — Jim

Jim is a personal training agent, multi-tenant: any number of athletes sign up
with email + password, each connecting their own Garmin account. Claude is the
reasoning engine — it talks to Garmin directly through the MCP server at
`/mcp` (`mcp_server.py`), reading real history/readiness and writing workouts,
reasoning about the next session within that athlete's knee/ankle constraints.
A nightly cron keeps Garmin history fresh (sync, reconcile, cleanup) but never
writes a plan itself; workouts reach the watch only when Claude is explicitly
asked to push one.
Not a product — real accounts and real per-user isolation, but one deployment,
one Postgres, one operator.
Backend-only: this is a pure JSON API with no HTML/frontend routes. The old
inline HTML/CSS/JS pages have been scrapped; a new UI will be built separately
against the API described below.

**The old chat path is gone.** `coach.py` (its own conversation loop),
`playbook.py` (the per-user template library), `agent/validate.py` (the
code-enforced guardrail), and the `/chat/*` + `/api/playbook` routes have been
removed now that the MCP path is verified end-to-end. The one piece of
Jim-side state the MCP path needs is the small `constraints` table (knee/ankle
limits, standing rules, goals) — everything the old playbook's template
library used to hold now lives in Garmin's own workout library, and safety
lives in `skills/jim-coach/SKILL.md` instead of a code guardrail.

## Module table

| Path | Purpose | Status |
|---|---|---|
| `api/index.py`, `vercel.json` | Serverless entrypoint + deploy config | active |
| `src/jim/app.py` | FastAPI app, `/health`, `/api/cron/nightly`, mounts the MCP app at `/mcp`, wires in `web/` routers | active |
| `src/jim/mcp_server.py` | Garmin MCP — read history/readiness/calendar/workout library, write create/schedule/unschedule, get/set constraints. Bearer-token auth, re-resolved per call (see its docstring for why) | active |
| `skills/jim-coach/SKILL.md` | Operating instructions for Claude when it's the one calling the MCP tools — constraints-first, data-grounded recommendations, never write without an explicit ask, `set_constraints` is a full replace. This is the safety layer now that there's no code guardrail | active |
| `src/jim/web/{auth,garmin,constraints}_routes.py`, `deps.py` | Pure JSON API routes (no HTML). `auth_routes` also returns a bearer token on login/signup for non-browser clients (the MCP server) | active |
| `src/jim/schemas.py` | Typed contracts, incl. `StructuredSession` | active |
| `src/jim/config.py`, `db.py`, `auth.py`, `crypto.py` | Settings, Postgres/`kv`, auth, at-rest credential encryption | active |
| `src/jim/tools/garmin.py` | Garmin reads/writes + workout scheduling + exercise-taxonomy matching | active |
| `src/jim/tools/exercise_match.py` | LLM fallback for unmatched exercise names, validated against the vendored taxonomy | active |
| `src/jim/tools/history.py` | Deterministic features + readiness read | active |
| `src/jim/tools/research.py` | Corpus/Tavily research (`research_training`) | needs-review — not exposed as an MCP tool in `mcp_server.py`; nothing calls it now that `coach.py` is gone, see Unresolved |
| `src/jim/tools/memory.py` | Suggestion/outcome recording (`record_suggestion`, `record_outcome`); used by `jobs/reconcile.py` | needs-review — nothing calls `record_suggestion` now that `coach.py` is gone, see Unresolved |
| `src/jim/jobs/nightly.py` | Sync + reconcile + cleanup cron; never drafts a plan | active |
| `src/jim/jobs/reconcile.py` | Matches Garmin actuals to stored suggestions | needs-review — depends on `suggestions` rows nothing currently writes, see Unresolved |
| `src/jim/migrations/001–012_*.sql` | Additive, idempotent, never edited after applied. `012_drop_playbooks.sql` drops `007_users.sql`'s `playbooks` table now that nothing reads or writes it | active |
| `src/jim/data/garmin_exercises.json` | Vendored Garmin exercise taxonomy | active |
| `data/corpus/*` | Research corpus source + template | needs-review — not traced to `research.py` ingestion path this session |
| `scripts/backfill_users.py` | One-off: creates the original athlete's user row, backfills `user_id` onto pre-multi-tenant rows. No longer touches a playbook. Not idempotent by design | active (one-off, already run) |
| `scripts/backfill.py` | Repeatable ~90-day Garmin history backfill for an existing user. Idempotent | active |
| `scripts/exercise_map.py`, `garmin_login.py`, `make_icon.py`, `refresh_garmin_exercises.py`, `seed_corpus.py` | Ops/dev utilities | needs-review — not individually verified this session |
| `tests/*` | Offline, fixture/fake-driven, one file per module + `test_multi_user_isolation.py` (load-bearing) | active |
| `docs/architecture.md`, `docs/garmin_strength.md`, `README.md`, `HANDOFF.md`, `DEPLOY.md` | Architecture/payload-format/deploy docs | active |

## Architecture rules (from the code, not assumed)

- **One planning path.** Claude, through the MCP server, is the only thing that writes a plan or pushes to Garmin. `jobs/nightly.py` only syncs/reconciles/cleans up — it never drafts anything.
- **Propose-only.** Nothing pushes to Garmin except an explicit write-tool call (`create_or_update_workout`, `save_to_library`, `schedule_workout`) made in response to an unambiguous ask — see `skills/jim-coach/SKILL.md` rules 3–4.
- **No code-enforced guardrail.** There is no `validate.py` behind the MCP tools; `skills/jim-coach/SKILL.md` (constraints-first, ground every recommendation in real history, never write without an explicit ask) is the safety layer. Read it before treating any MCP write tool as safe by default.
- **Named workouts live in Garmin, not Jim.** There is no Jim-owned template/rotation store anymore — `list_saved_workouts`/`get_saved_workout` read the athlete's real Garmin library directly.
- **User isolation is structural, not conventional.** Every history table and `kv` row carries `user_id` in a composite key — see `tests/test_multi_user_isolation.py`.
- **Migrations are append-only** — `00N_*.sql`, never edited post-apply.

## Commands

```bash
ruff check . && pytest          # offline test suite
uvicorn jim.app:app --reload    # JSON API at /auth, /api/*, MCP at /mcp
python -m jim.jobs.nightly      # nightly housekeeping, by hand
```

**Unresolved:**
- `tools/memory.record_suggestion`, `tools/history.last_rotation_key`, and
  all of `tools/research.py` (`research_training`) have no callers left now
  that `coach.py` is gone — `mcp_server.py` exposes no research tool, and
  the MCP path never calls `record_suggestion`, so `suggestions` gets no new
  rows and `jobs/reconcile.py`'s adherence matching (which reads
  `suggestions`) is effectively dark for MCP-created workouts. Whether
  research/adherence tracking should be wired into MCP tools, or this code
  should be retired too, wasn't decided this session — flagged rather than
  guessed at.
- Whether `data/corpus/*` and the five untraced `scripts/*` utilities are
  still exercised by any current path.
