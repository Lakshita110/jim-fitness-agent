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

There are three distinct memory stores now, deliberately not merged: `constraints`
is one athlete's own limits (write access: that athlete's own sessions, via
`set_constraints`); `research_corpus` is scientific training literature
(write access: operator-curated only, via `scripts/seed_corpus.py`);
`technical_notes` is a cross-user log of tool-usage/system mistakes Claude
catches while coaching (write access: any athlete's session, via
`report_technical_issue` — self-service, unlike the other two, since these
are advisory operational notes rather than safety rules or scientific
claims). Self-editing `skills/jim-coach/SKILL.md` itself was deliberately
ruled out — it's the safety layer, and letting the model rewrite the rules
that constrain it based on its own inference about its mistakes was judged
too risky to automate.

## Module table

| Path | Purpose | Status |
|---|---|---|
| `api/index.py`, `vercel.json` | Serverless entrypoint + deploy config | active |
| `src/jim/app.py` | FastAPI app, `/health`, `/api/cron/nightly`, mounts the MCP app at `/mcp`, wires in `web/` routers | active |
| `src/jim/mcp_server.py` | Garmin MCP — read history/readiness/calendar/workout library, write create/schedule/unschedule, get/set constraints, `research_training` (science + technical-notes lookups via `domain=`), `report_technical_issue`. Kept to 17 tools total on purpose — a new lookup got folded into `research_training`'s `domain` param rather than adding another tool, since more tools costs more context per call and more chances for the model to pick the wrong one. Bearer-token auth, re-resolved per call (see its docstring for why) | active |
| `skills/jim-coach/SKILL.md` | Operating instructions for Claude when it's the one calling the MCP tools — constraints-first, data-grounded recommendations, never write without an explicit ask, `set_constraints` is a full replace. This is the safety layer now that there's no code guardrail | active |
| `src/jim/web/{auth,garmin,constraints}_routes.py`, `deps.py` | Pure JSON API routes (no HTML). `auth_routes` also returns a bearer token on login/signup for non-browser clients (the MCP server) | active |
| `src/jim/schemas.py` | Typed contracts, incl. `StructuredSession` | active |
| `src/jim/config.py`, `db.py`, `auth.py`, `crypto.py` | Settings, Postgres/`kv`, `technical_notes` (cross-user tool-mistake log), auth, at-rest credential encryption | active |
| `src/jim/tools/garmin.py` | Garmin reads/writes + workout scheduling + exercise-taxonomy matching | active |
| `src/jim/tools/exercise_match.py` | LLM fallback for unmatched exercise names, validated against the vendored taxonomy | active |
| `src/jim/tools/history.py` | Deterministic features + readiness read | active |
| `src/jim/tools/research.py` | Corpus/Tavily research — pgvector search over `data/corpus/*`, Tavily to top up, both domain-restricted. Exposed as `research_training(domain="science")`; `domain="technical"` on the same tool instead reads `technical_notes` via `db.list_technical_notes` | active |
| `src/jim/tools/memory.py` | Suggestion/outcome recording (`record_suggestion`, `record_outcome`); used by `jobs/reconcile.py` | needs-review — nothing calls `record_suggestion` now that `coach.py` is gone, see Unresolved |
| `src/jim/jobs/nightly.py` | Sync + reconcile + cleanup cron; never drafts a plan | active |
| `src/jim/jobs/reconcile.py` | Matches Garmin actuals to stored suggestions | needs-review — depends on `suggestions` rows nothing currently writes, see Unresolved |
| `src/jim/migrations/001–013_*.sql` | Additive, idempotent, never edited after applied. `012_drop_playbooks.sql` drops `007_users.sql`'s `playbooks` table now that nothing reads or writes it; `013_technical_notes.sql` adds the cross-user tool-mistake log | active |
| `src/jim/data/garmin_exercises.json` | Vendored Garmin exercise taxonomy | active |
| `data/corpus/*` | Research corpus source markdown, seeded via `scripts/seed_corpus.py` into `research_corpus` (pgvector). Shared across every athlete — general training science, not any one athlete's protocol; per-athlete specifics stay in `constraints` | active |
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
- `tools/memory.record_suggestion` and `tools/history.last_rotation_key`
  still have no callers now that `coach.py` is gone — the MCP path never
  calls `record_suggestion`, so `suggestions` gets no new rows and
  `jobs/reconcile.py`'s adherence matching (which reads `suggestions`) is
  effectively dark for MCP-created workouts. Whether adherence tracking
  should be wired into an MCP write tool, or this code should be retired
  too, wasn't decided this session — flagged rather than guessed at.
- `data/corpus/*` currently has three seed documents (pain-monitoring
  model, patellofemoral pain load management, isometric loading for tendon
  pain) — original summaries of well-established public training-science
  concepts, written to unblock `research_training` end-to-end, not a
  substitute for properly curated primary sources. Whether/when to run
  `scripts/seed_corpus.py` against the deployed DB (requires a live
  Postgres with pgvector + `OPENROUTER_API_KEY`) wasn't done this session.
- Whether the five untraced `scripts/*` utilities (`exercise_map.py`,
  `garmin_login.py`, `make_icon.py`, `refresh_garmin_exercises.py`, and now
  `seed_corpus.py` — verified this session but not yet run against a live
  DB) are still exercised by any current path.
