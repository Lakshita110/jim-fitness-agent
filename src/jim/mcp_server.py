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

from datetime import date

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
    "pilates": "mobility",
    "stretching": "mobility",
    "run": "running",
    "bike": "cycling",
    "swim": "swimming",
    "walk": "walking",
    "hike": "hiking",
}

_VALID_KINDS = (
    "strength", "conditioning", "mobility", "rest",
    "running", "cycling", "swimming", "walking", "hiking", "yoga", "other",
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
    push/steady/ease/rest, with the ACWR and recovery numbers behind it."""
    from jim.tools.history import readiness_read

    user_id = _current_user_id()
    _ensure_history(user_id)
    day = date.fromisoformat(as_of) if as_of else date.today()
    return readiness_read(user_id, day).model_dump(mode="json")


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
    """Recent Garmin activities (type, duration) for the trailing window."""
    from jim.tools.history import workout_history

    user_id = _current_user_id()
    _ensure_history(user_id)
    return workout_history(user_id, days=days)


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


# --- TEMP: probing real Garmin sportTypeIds live, remove after use -----------


@mcp.tool
def _debug_probe_sport_id(for_date: str, sport_id: int) -> dict:
    """TEMPORARY. Create a minimal workout tagged with a raw sportTypeId (not
    a named kind) so the response can be read back to find out what Garmin
    actually calls that id — there's no official docs for this, only
    conflicting reverse-engineered lists."""
    from jim.tools.garmin import client

    api = client(_current_user_id())
    payload = {
        "workoutName": f"PROBE {sport_id}",
        "sportType": {"sportTypeId": sport_id, "sportTypeKey": "probe"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": sport_id, "sportTypeKey": "probe"},
            "workoutSteps": [{
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": 600,
                "description": "probe",
            }],
        }],
    }
    resp = api.upload_workout(payload)
    return {"workout_id": str(resp.get("workoutId", "")), "raw": resp.get("sportType")}


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
    cycling, swimming, walking, hiking, yoga, other — pick the specific one
    that matches the session rather than defaulting to conditioning; a plain
    walk should be `kind="walking"`, not "conditioning". Garmin's own
    sportType vocabulary (e.g. "strength_training", "run") is also accepted
    and mapped automatically if that's what you read off get_scheduled_
    workouts/list_saved_workouts, but prefer the exact names above when
    you're the one choosing. strength/mobility steps get matched against
    Garmin's exercise library (category + exerciseName); every other kind is
    treated as a plain activity and just carries its description.

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
