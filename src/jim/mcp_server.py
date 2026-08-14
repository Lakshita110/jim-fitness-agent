"""Jim's Garmin MCP — the replacement for coach.py's own conversation engine.

Claude is now the reasoning engine; this server just gives it hands: read
Garmin history/readiness/calendar/workout library, write (create/schedule/
unschedule) workouts, and read/edit the one remaining piece of Jim-side
state — a per-athlete constraints doc (knee/ankle limits, standing rules,
goals) that replaced the old playbook's template library. Named/reusable
workouts now live in Garmin's own library, not a separate YAML store.

Auth: no cookie jar here (this isn't a browser), so every tool call resolves
its caller from the same signed token `auth.py` already issues from
/auth/login — as `Authorization: Bearer <token>` when the client can set
headers, or `?token=<token>` on the connector URL when it can't (see
`_token_from_request`; claude.ai's own connector UI is the latter case).
Read per-call via `get_http_headers()`/`get_http_request()` rather than
cached anywhere, and the server is mounted stateless (`stateless_http=True`)
so each HTTP request is independent. Both choices are deliberate: FastMCP
has a documented bug where a stateful StreamableHTTP session can leak a
*stale* request's context into a later tool call on the same MCP session —
unacceptable when two different people (different `user_id`s) are calling
the same deployed server. Every tool re-resolves the caller fresh; nothing
about identity is ever cached across calls. See tests/test_mcp_server.py
for the isolation check this depends on.
"""

from datetime import date, timedelta

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request
from pydantic import BaseModel

from jim import auth, db
from jim.schemas import ExerciseStep, SessionKind, StructuredSession
from jim.tools.garmin import ADAPTED_WORKOUT_PREFIX

# Claude reasonably reaches for Garmin's own vocabulary (it just read it from
# get_scheduled_workouts/list_saved_workouts, which report sportType keys
# like "strength_training") rather than SessionKind's own spelling of the
# same thing — normalize instead of a raw pydantic 422 that gives no
# indication of what to try next.
_KIND_ALIASES: dict[str, SessionKind] = {
    "strength_training": "strength",
    "fitness_equipment": "strength",
    "cardio": "conditioning",
    "cardio_training": "conditioning",
    "stretching": "mobility",
    "run": "running",
    "bike": "cycling",
    "swim": "swimming",
    "walk": "walking",
    "hike": "hiking",
    "ruck": "rucking",
}

_VALID_KINDS = (
    "strength", "conditioning", "mobility", "rest",
    "running", "cycling", "swimming", "walking", "hiking", "yoga", "pilates",
    "hiit", "rucking", "other",
)


def _normalize_kind(kind: str) -> SessionKind:
    if kind in _VALID_KINDS:
        return kind  # type: ignore[return-value]
    normalized = _KIND_ALIASES.get(kind.strip().lower())
    if normalized is None:
        raise ToolError(
            f"unrecognized kind {kind!r} — use one of {', '.join(_VALID_KINDS)}"
        )
    return normalized

mcp = FastMCP("jim-garmin")


def _token_from_request() -> str:
    """`Authorization: Bearer <token>` if present, else a `?token=` query
    param on the connector URL.

    The header is the correct transport, but claude.ai's own "Add custom
    connector" dialog only exposes OAuth Client ID/Secret fields — there is
    no way to set a request header from that UI (confirmed via
    anthropics/claude-ai-mcp#112 and #411, not a gap in our setup). The URL
    field is freely editable, so the query param is the practical fallback
    for that specific client; both paths resolve through the same
    `auth.verify_session_token`, so neither is treated as more trusted."""
    # get_http_headers() strips Authorization by default (it's meant for
    # safely forwarding headers downstream) — has to be opted back in.
    header = get_http_headers(include={"authorization"}).get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return get_http_request().query_params.get("token", "")


def _current_user_id() -> int:
    """Resolve the caller from the bearer token on *this* request. Never
    cached — see the module docstring for why that matters here."""
    token = _token_from_request()
    if not token:
        raise ToolError(
            "missing token — pass Authorization: Bearer <token>, or ?token=<token>"
            " on the connector URL if your client can't set headers"
        )
    user_id = auth.verify_session_token(token)
    if user_id is None:
        raise ToolError("invalid or expired token — sign in again to mint a new one")
    db.ensure_migrated()
    return user_id


def _ensure_history(user_id: int) -> None:
    """First real read for a user with zero garmin_daily rows triggers the
    same 90-day pull the nightly cron would eventually do on its own — see
    tools.garmin.backfill_if_empty. Called from the read tools rather than
    _current_user_id() itself so a write-only call (e.g. set_constraints)
    doesn't pay for it; every read tool needs the history anyway."""
    from jim.jobs.nightly import _today_for_user
    from jim.tools.garmin import backfill_if_empty

    try:
        backfill_if_empty(user_id, _today_for_user(user_id))
    except Exception:
        # Best-effort — a Garmin hiccup here must not block the read the
        # athlete actually asked for.
        pass


class StepIn(BaseModel):
    exercise: str
    sets: int = 1
    reps: int | None = None
    duration_sec: int | None = None
    weight_kg: float | None = None


# --- read: history, readiness, calendar, workout library --------------------


@mcp.tool
def get_readiness(as_of: str | None = None) -> dict:
    """Today's (or `as_of`, ISO date) training-load + recovery verdict —
    push/steady/ease/rest, with the ACWR and recovery numbers behind it.

    Also includes `training_readiness` and `training_status`, Garmin's own
    (differently computed) readiness verdict and training-load
    classification (productive, peaking, overreaching, detraining,
    unproductive, ...) — a second opinion alongside Jim's own ACWR-based
    verdict above them. Either can come back empty/null if Garmin hasn't
    computed it for this athlete's watch/history yet; that's real, not a
    bug, and just means less to go on from that source today."""
    from jim.tools.garmin import get_training_readiness, get_training_status
    from jim.tools.history import readiness_read

    user_id = _current_user_id()
    _ensure_history(user_id)
    day = date.fromisoformat(as_of) if as_of else date.today()
    result = readiness_read(user_id, day).model_dump(mode="json")
    result["training_readiness"] = get_training_readiness(user_id, day)
    result["training_status"] = get_training_status(user_id, day)
    return result


@mcp.tool
def get_exercise_history(exercise: str, days: int = 180) -> str:
    """How the athlete actually performed a movement recently (sets/reps/kg
    per session) — fuzzy-matched against logged Garmin sets."""
    from jim.tools.history import exercise_history

    user_id = _current_user_id()
    _ensure_history(user_id)
    return exercise_history(user_id, exercise, days=days)


@mcp.tool
def get_recent_activities(days: int = 14) -> str:
    """Recent Garmin activities (type, duration) for the trailing window,
    plus a daily step count line per day — general daily activity context,
    not structured training."""
    from jim.tools.garmin import get_daily_steps
    from jim.tools.history import workout_history

    user_id = _current_user_id()
    _ensure_history(user_id)
    text = workout_history(user_id, days=days)

    today = date.today()
    steps = get_daily_steps(user_id, today - timedelta(days=days), today)
    if steps:
        step_lines = "\n".join(
            f"{s['calendarDate']}: {s['totalSteps']} steps (goal {s['stepGoal']})"
            for s in steps
        )
        text = f"{text}\n\nDaily steps:\n{step_lines}"
    return text


@mcp.tool
def get_scheduled_workouts(start: str, end: str) -> list[dict]:
    """Workouts already on the Garmin calendar between `start` and `end`
    (ISO dates, inclusive) — what's actually scheduled, not what Jim thinks
    it pushed."""
    from jim.tools.garmin import get_scheduled_workouts as _get

    user_id = _current_user_id()
    rows = _get(user_id, date.fromisoformat(start), date.fromisoformat(end))
    return [{**r, "date": r["date"].isoformat()} for r in rows]


@mcp.tool
def list_saved_workouts() -> list[dict]:
    """The athlete's personal Garmin workout library — named, reusable
    workouts (create/edit them with `create_or_update_workout`)."""
    from jim.tools.garmin import list_garmin_workouts

    return list_garmin_workouts(_current_user_id())


@mcp.tool
def get_saved_workout(workout_id: str) -> dict:
    """Full step-by-step detail for one saved Garmin workout."""
    from jim.tools.garmin import get_garmin_workout_detail

    return get_garmin_workout_detail(_current_user_id(), workout_id)


# --- write: create/schedule/unschedule ---------------------------------------


@mcp.tool
def create_or_update_workout(
    for_date: str, title: str, kind: str, steps: list[StepIn]
) -> dict:
    """Create a new Garmin workout from structured steps (exercise, sets,
    reps or duration_sec, weight_kg) and return its `workout_id`. Garmin has
    no in-place edit for structured workouts — to "update" one, create a new
    version and `schedule_workout` it in place of the old (re-scheduling a
    day replaces what was there, it doesn't duplicate).

    `kind` is one of: strength, conditioning, mobility, rest, running,
    cycling, swimming, walking, hiking, yoga, pilates, hiit, rucking, other —
    pick the specific one that matches the session rather than defaulting to
    conditioning; a plain walk should be `kind="walking"`, not "conditioning".
    ("hiking" has no dedicated Garmin sportType and is stored as "other" —
    still fine to use, just know that's what it becomes on Garmin's side.)
    Garmin's own sportType vocabulary (e.g. "strength_training", "run") is also accepted
    and mapped automatically if that's what you read off get_scheduled_
    workouts/list_saved_workouts, but prefer the exact names above when
    you're the one choosing. strength/mobility steps get matched against
    Garmin's exercise library (category + exerciseName); every other kind is
    treated as a plain activity and just carries its description — meaning
    for those kinds, whatever you put in `exercise` is exactly what the
    athlete sees on their watch, verbatim, with no matching to smooth over a
    vague name. Write it like a real step ("Easy run", "Brisk walk", "Tempo
    intervals"), not a placeholder like "Go" or "Exercise".

    The title is auto-prefixed ("Jim · ...") so this one-off adaptation is
    distinguishable from the athlete's real saved workouts (Full Body A,
    PT Day, etc.) and gets swept automatically once its date has passed —
    see jobs/nightly.py's cleanup_adapted_workouts. Don't use this for a
    workout meant to stick around in the athlete's library; that's a
    library edit on Garmin itself (create_or_update_workout is for a
    single day's session, not a template)."""
    from jim.tools.garmin import create_garmin_workout

    user_id = _current_user_id()
    session = StructuredSession(
        for_date=date.fromisoformat(for_date),
        kind=_normalize_kind(kind),
        title=f"{ADAPTED_WORKOUT_PREFIX}{title}",
        steps=[ExerciseStep(**s.model_dump()) for s in steps],
    )
    ref = create_garmin_workout(user_id, session)
    return ref.model_dump(mode="json")


@mcp.tool
def save_to_library(title: str, kind: str, steps: list[StepIn]) -> dict:
    """Create a PERMANENT Garmin workout, meant to stick around and be
    reused — e.g. "Full Body A", "PT Day" — not a one-off adaptation for a
    single day. Unlike create_or_update_workout, the title is NOT prefixed
    and this workout is never swept by the nightly/on-demand cleanup; it's
    indistinguishable from anything the athlete built by hand in Garmin
    Connect. Only call this on an explicit ask to add or save something to
    the library ("save this as a template," "add this to my workouts") —
    never as a byproduct of planning a single day's session, and never
    silently; tell the athlete you're about to create a permanent library
    entry before you do it, same as any other write.

    Garmin has no in-place edit for a saved workout. To change one that
    already exists: call this again with the corrected steps (a new
    workout_id comes back), point any days that had the old one scheduled
    at the new id via schedule_workout, then delete_workout the old id once
    you've confirmed the athlete wants it gone — don't delete it first.

    Same `kind`/step rules as create_or_update_workout (see its docstring
    for the full list and the strength/mobility-only exercise matching)."""
    from jim.tools.garmin import create_garmin_workout

    user_id = _current_user_id()
    session = StructuredSession(
        for_date=date.today(),
        kind=_normalize_kind(kind),
        title=title,
        steps=[ExerciseStep(**s.model_dump()) for s in steps],
    )
    ref = create_garmin_workout(user_id, session)
    return ref.model_dump(mode="json")


@mcp.tool
def schedule_workout(workout_id: str, on: str) -> dict:
    """Schedule an existing Garmin workout (by id) onto the calendar for
    `on` (ISO date). Only ever call this on an explicit ask to push/schedule
    — never as a side effect of just discussing a plan."""
    from jim.tools.garmin import schedule_workout as _schedule

    _schedule(_current_user_id(), workout_id, date.fromisoformat(on))
    return {"ok": True}


@mcp.tool
def unschedule_day(on: str) -> dict:
    """Clear whatever's scheduled (not completed) on `on` (ISO date) —
    used before re-pushing a replacement so the day doesn't end up with two
    workouts."""
    from jim.tools.garmin import clear_schedule

    clear_schedule(_current_user_id(), date.fromisoformat(on))
    return {"ok": True}


@mcp.tool
def delete_workout(workout_id: str) -> dict:
    """Delete a Garmin workout outright (not just unschedule it) — for a
    one-off adaptation that's no longer wanted."""
    from jim.tools.garmin import delete_garmin_workout

    delete_garmin_workout(_current_user_id(), workout_id)
    return {"ok": True}


@mcp.tool
def backfill_history(days: int = 90) -> dict:
    """Force a re-pull of the trailing `days` of Garmin history (daily
    metrics, activities, exercise sets) into Jim's database, even if some
    history already exists. Every other read tool auto-backfills once on an
    account's first-ever call, but only when it has zero history — an
    account that connected Garmin before that existed (or only ever synced
    a few days) won't get topped up automatically. Call this on an explicit
    ask like "backfill my history" or "pull in my past workouts." Runs
    synchronously and does ~90 sequential Garmin calls, so it can take a
    couple of minutes — say so before calling it."""
    from jim.jobs.nightly import _today_for_user
    from jim.tools.garmin import backfill_history as _backfill

    user_id = _current_user_id()
    _backfill(user_id, _today_for_user(user_id), days)
    return {"ok": True}


@mcp.tool
def cleanup_old_adapted_workouts(lookback_days: int = 30) -> dict:
    """Delete past one-off workouts this server created (titled "Jim · ...")
    so they don't accumulate in the athlete's Garmin library/watch app. Runs
    automatically every night, but call this directly if asked to "clean up"
    or "tidy up" now rather than waiting for the nightly run. Only touches
    workouts on or before yesterday; today's and future scheduled days are
    never swept."""
    from jim.jobs.nightly import _today_for_user, cleanup_adapted_workouts

    user_id = _current_user_id()
    cleanup_adapted_workouts(user_id, _today_for_user(user_id), lookback_days)
    return {"ok": True}


# --- research: grounded lookups, shared across every athlete -----------------


@mcp.tool
def research_training(question: str, domain: str = "science") -> list[dict]:
    """Look up grounded snippets for a question that needs more than opinion
    — general training science shared across every athlete Jim coaches, NOT
    this athlete's specifics (always combine with get_constraints for what
    actually applies to who you're talking to) and NOT the athlete's own
    training mistakes (that belongs in their constraints if it changes a
    standing rule).

    `domain="science"` (default) — a scientific/training-load question, e.g.
    "how should I load an irritated patellar tendon" or "how much weekly
    volume before overtraining risk goes up." Searches a curated corpus of
    vetted articles/clinical guidelines first, then tops up with a
    domain-restricted web search. Each hit carries a `source`; cite it
    rather than presenting it as your own claim.

    `domain="technical"` — has this exact system/tool-usage mistake been
    caught before, by you or another session? A Garmin quirk, an
    exercise-matching miss, a `kind` that mapped somewhere unexpected.
    Keyword search (not semantic) over a shared, cross-user notes log — pass
    the term you'd search for, not a full sentence. Cheap to check before a
    write you're unsure about.

    Either domain may return an empty list — that's a real answer, not a
    failure; say so rather than inventing a citation or assuming nothing's
    ever gone wrong."""
    _current_user_id()  # require sign-in, same as every other tool
    if domain == "technical":
        from jim import db as _db

        return [
            {**row, "created_ts": row["created_ts"].isoformat()}
            for row in _db.list_technical_notes(query=question)
        ]
    from jim.tools.research import research_training as _research

    return [hit.model_dump(mode="json") for hit in _research(question)]


@mcp.tool
def report_technical_issue(title: str, note: str, tags: list[str] | None = None) -> dict:
    """Log a technical/tool-usage mistake to the shared cross-user notes log
    (searchable later via research_training(domain="technical")) — call this
    when you catch a real system-level surprise: a tool returning something
    you didn't expect, a Garmin quirk, an exercise name that matched wrong,
    a `kind` that landed somewhere unintended. This is NOT for the athlete's
    own training mistakes or setbacks (that belongs in their constraints, if
    it changes a standing rule) and NOT a place to record a scientific claim
    (that's the research corpus, human-curated only). Write `note` so a
    future session — for a different athlete — understands the mistake and
    what to do differently, not just what went wrong for this one
    conversation. Every athlete's Claude session can write and read this
    log, so keep entries genuinely general rather than specific to this
    athlete's situation."""
    from jim import db as _db

    user_id = _current_user_id()
    _db.add_technical_note(title, note, tags or [], user_id)
    return {"ok": True}


# --- constraints: the one remaining piece of Jim-side memory -----------------


@mcp.tool
def get_constraints() -> str:
    """The athlete's standing knee/ankle limits, safety rules, and goals.
    Read this before proposing any session — it's the safety authority now
    that there's no code-enforced guardrail."""
    return db.get_constraints(_current_user_id())


@mcp.tool
def set_constraints(content: str) -> dict:
    """Rewrite the athlete's constraints doc (whole-document replace, not a
    merge) — call this when they state a new limit, rule, or long-term goal."""
    db.set_constraints(_current_user_id(), content)
    return {"ok": True}


def build_asgi_app():
    """Mounted at /mcp in app.py (`app.mount("/mcp", ...)`), so this app's own
    internal path is left at the root — mounting handles where it's exposed.
    `stateless_http=True` is load-bearing — see the module docstring."""
    return mcp.http_app(path="/", stateless_http=True)
