"""Jim's Garmin MCP server — Claude is the coach; this gives it hands.

Tools, by group:
- read: get_readiness, get_exercise_history, get_recent_activities,
  get_scheduled_workouts, list_saved_workouts, get_saved_workout
- write: create_or_update_workout (one-off, dated, auto-scheduled),
  save_to_library (permanent), update_workout (in place, by id),
  schedule_workout, unschedule_day, delete_workout
- maintenance: backfill_history, cleanup_old_adapted_workouts
- memory: get_constraints / set_constraints — the one piece of Jim-side
  state (knee/ankle limits, standing rules, goals). Named/reusable
  workouts live in Garmin's own library, not here.

Every tool validates its inputs up front and turns bad input or a Garmin
failure into a ToolError with a message saying what to do next — see
`_garmin` and the `_parse_*` helpers — rather than letting a raw Python or
HTTP exception reach the model.

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

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, timedelta
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request
from pydantic import BaseModel

from jim import auth, db
from jim.schemas import ExerciseStep, SessionKind, StructuredSession
from jim.tools import memory
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


log = logging.getLogger(__name__)

LIVE_SYNC_INTERVAL = timedelta(minutes=15)

mcp = FastMCP("jim-garmin")


# --- input validation / error translation ------------------------------------


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as e:
        raise ToolError(f"{field} must be an ISO date like 2026-09-24, got {value!r}") from e


def _parse_workout_id(value: str | int) -> str:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ToolError(
            f"workout_id must be Garmin's numeric id (from list_saved_workouts or"
            f" get_scheduled_workouts), got {value!r}"
        )
    return text


def _bounded(value: int, field: str, lo: int, hi: int) -> int:
    if not lo <= value <= hi:
        raise ToolError(f"{field} must be between {lo} and {hi}, got {value}")
    return value


def _user_today(user_id: int) -> date:
    """'Today' in the athlete's own timezone — not the server's UTC date,
    which is already tomorrow for a US athlete by late evening."""
    from jim.jobs.nightly import _today_for_user

    return _today_for_user(user_id)


@contextmanager
def _garmin(user_id: int, action: str) -> Iterator[None]:
    """Turn any Garmin-side failure into a ToolError that says what to do.

    An auth failure also evicts the cached client, so the next call logs in
    fresh instead of reusing a dead session for the rest of the process."""
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    from jim.tools import garmin as garmin_tools

    try:
        yield
    except ToolError:
        raise
    except GarminConnectAuthenticationError as e:
        garmin_tools._clients.pop(user_id, None)
        raise ToolError(
            f"couldn't {action}: Garmin rejected the stored login — reconnect Garmin"
            " in Jim's settings, then retry"
        ) from e
    except GarminConnectTooManyRequestsError as e:
        raise ToolError(
            f"couldn't {action}: Garmin is rate-limiting requests — wait a few minutes"
        ) from e
    except GarminConnectConnectionError as e:
        raise ToolError(f"couldn't {action}: Garmin returned an error ({e})") from e
    except RuntimeError as e:
        # tools.garmin.client() raises RuntimeError with an already-readable
        # message (not connected, bad token blob, login failed).
        garmin_tools._clients.pop(user_id, None)
        raise ToolError(f"couldn't {action}: {e}") from e
    except Exception as e:
        log.exception("garmin call failed: %s (user %s)", action, user_id)
        raise ToolError(f"couldn't {action}: {type(e).__name__}: {e}") from e


def _best_effort(user_id: int, action: str, fn: Any, *args: Any) -> Any:
    """For optional extras inside a read (Garmin's own readiness, step
    counts): a failure there shouldn't sink the whole response. Returns a
    small {"unavailable": reason} marker instead so the model knows the
    field is missing rather than genuinely empty."""
    try:
        return fn(user_id, *args)
    except Exception as e:  # noqa: BLE001
        log.warning("optional garmin read failed: %s (user %s): %s", action, user_id, e)
        return {"unavailable": f"{type(e).__name__}: {e}"}


def _remember(action: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """Keep the plan-vs-actual record in step with a Garmin write that has
    already succeeded. Failing here must not turn a completed write into an
    error (the athlete's watch already has the change), so it's logged."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.warning("couldn't record plan change (%s)", action, exc_info=True)


def _record_scheduled(user_id: int, workout_id: str, day: date) -> None:
    """schedule_workout only knows an id — read the workout back so the
    stored plan has its kind, title and planned reps."""
    from jim.tools.garmin import get_garmin_workout_detail, plan_from_garmin_detail

    detail = get_garmin_workout_detail(user_id, workout_id)
    memory.record_plan(user_id, workout_id, plan_from_garmin_detail(detail, day))


def _to_steps(steps: list["StepIn"], kind: str) -> list[ExerciseStep]:
    if not steps and kind != "rest":
        raise ToolError("steps is empty — a workout needs at least one step")
    out = []
    for i, s in enumerate(steps, 1):
        if not s.exercise.strip():
            raise ToolError(f"step {i}: exercise name is empty")
        if s.sets < 1:
            raise ToolError(f"step {i} ({s.exercise}): sets must be at least 1")
        if s.pyramid_group is not None and not s.pyramid_rounds:
            raise ToolError(
                f"step {i} ({s.exercise}): pyramid_group is set but pyramid_rounds isn't"
                " — say how many outer rounds the block repeats"
            )
        for field in ("reps", "duration_sec", "end_at_heart_rate_bpm"):
            value = getattr(s, field)
            if value is not None and value <= 0:
                raise ToolError(f"step {i} ({s.exercise}): {field} must be positive")
        if s.distance_m is not None and s.distance_m <= 0:
            raise ToolError(f"step {i} ({s.exercise}): distance_m must be positive")
        if s.weight_kg is not None and s.weight_kg < 0:
            raise ToolError(f"step {i} ({s.exercise}): weight_kg can't be negative")
        for field in ("target_heart_rate_zone", "target_power_zone"):
            zone = getattr(s, field)
            if zone is not None and not 1 <= zone <= 10:
                raise ToolError(f"step {i} ({s.exercise}): {field} must be 1-10, got {zone}")
        out.append(ExerciseStep(**s.model_dump()))
    return out


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
    doesn't pay for it; every read tool needs the history anyway.

    Also re-syncs *today's* row on every call (sync_today, a single cheap
    Garmin round trip) — not just when history is empty. The nightly cron
    runs once, early evening, which is BEFORE that night's sleep/HRV/body-
    battery data even exists yet; today's stored row is structurally stale
    until something re-fetches it later in the day or the following
    morning, and nothing did until now. A live chat read is exactly the
    moment worth paying that one extra call for — the athlete is asking
    right now, not waiting for tonight's cron.

    Throttled to once per LIVE_SYNC_INTERVAL per user: one coaching turn
    typically calls several read tools back to back, and re-fetching the same
    day from Garmin for each was slow and courted rate limits."""
    from datetime import datetime

    from jim.jobs.nightly import _today_for_user, sync_today
    from jim.tools.garmin import backfill_if_empty

    try:
        last = db.kv_get(user_id, "live_sync_at")
        now = datetime.now(UTC)
        if last and now - datetime.fromisoformat(last) < LIVE_SYNC_INTERVAL:
            return
        today = _today_for_user(user_id)
        backfill_if_empty(user_id, today)
        sync_today(user_id)
        db.kv_set(user_id, "live_sync_at", now.isoformat())
    except Exception:
        # Best-effort — a Garmin hiccup here must not block the read the
        # athlete actually asked for, but it must not vanish either.
        log.warning("history refresh failed for user %s", user_id, exc_info=True)


class StepIn(BaseModel):
    exercise: str
    sets: int = 1
    reps: int | None = None
    duration_sec: int | None = None
    weight_kg: float | None = None
    # Consecutive steps sharing the same superset_group (an arbitrary int
    # you choose, e.g. 1, 2, 3...) are wrapped together as one round on the
    # watch — "Round 1/3: Wall sit -> Row" — instead of each getting its own
    # separate repeat block. Leave unset for a normal single-exercise step.
    # All steps in a group should share the same `sets` value (the shared
    # round count); the first one wins if they don't agree.
    superset_group: int | None = None
    # Consecutive steps sharing the same pyramid_group (and pyramid_rounds)
    # get wrapped in an OUTER repeat block around whatever they'd normally
    # build — a repeat-of-repeats. Use for a genuinely nested structure
    # ("for 3 total rounds: 2x8 squats, then 12 lunges" — squats doubled
    # INSIDE each of the 3 outer rounds), which superset_group alone can't
    # express. Independent of superset_group — can combine both if the
    # inner block is itself a superset.
    pyramid_group: int | None = None
    pyramid_rounds: int | None = None
    # True press-to-continue rest (Garmin's own rest stepType) instead of a
    # fixed timer — see save_to_library/create_or_update_workout's docstring.
    self_paced_rest: bool = False
    # Garmin's own step role, not just descriptive text — "warmup"/"cooldown"/
    # "recovery"/"other"/"main" show correctly on the watch instead of being
    # lumped in as a generic interval. Default "interval" is the old, only,
    # behavior. "other"/"main" have no documentation anywhere; found by
    # directly probing stepTypeIds beyond the commonly-cited 1-6.
    role: Literal["warmup", "interval", "cooldown", "recovery", "other", "main"] = "interval"
    # Distance-based ending in meters ("run 5km"), instead of reps/duration_sec.
    distance_m: float | None = None
    # Ends the step once heart rate reaches this bpm instead of a fixed
    # time — "recover until HR drops to 130," typically with role="recovery".
    # Whether Garmin treats this as until-at/below or until-at/above wasn't
    # independently confirmed.
    end_at_heart_rate_bpm: int | None = None
    # Heart rate / power zone target (Garmin's own per-athlete zone number,
    # typically 1-5) — at most one should be set; heart rate wins if both are.
    target_heart_rate_zone: int | None = None
    target_power_zone: int | None = None
    # Pace target as a speed range in meters/second (see StructuredSession's
    # ExerciseStep docstring for the unit caveat — accepted by Garmin, but
    # not independently confirmed against what displays on the watch).
    target_pace_min_mps: float | None = None
    target_pace_max_mps: float | None = None
    # A second target riding alongside a primary one (target_heart_rate_zone
    # or target_power_zone) — e.g. HR zone as primary, cadence range on top.
    # Has no effect without a primary target set.
    secondary_target_cadence_min: float | None = None
    secondary_target_cadence_max: float | None = None


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
    bug, and just means less to go on from that source today. If Garmin
    itself errors on either, that field comes back as {"unavailable": ...}
    instead of failing the whole read. When body battery, HRV and sleep are
    all missing (typically early morning, before the watch syncs), a
    `recovery_note` says so — the verdict is then load-only.

    `as_of` defaults to today in the athlete's own timezone."""
    from jim.tools.garmin import get_training_readiness, get_training_status
    from jim.tools.history import readiness_read

    user_id = _current_user_id()
    _ensure_history(user_id)
    day = _parse_date(as_of, "as_of") if as_of else _user_today(user_id)
    result = readiness_read(user_id, day).model_dump(mode="json")
    if all(result.get(k) is None for k in ("body_battery", "hrv", "sleep_hours")):
        result["recovery_note"] = (
            "no body battery/HRV/sleep for this day yet — usually the watch hasn't"
            " synced last night's data to Garmin Connect. The verdict above is"
            " load-only; ask how they slept, or check again after a sync."
        )
    result["training_readiness"] = _best_effort(
        user_id, "training readiness", get_training_readiness, day,
    )
    result["training_status"] = _best_effort(
        user_id, "training status", get_training_status, day,
    )
    return result


@mcp.tool
def get_exercise_history(exercise: str, days: int = 180) -> str:
    """How the athlete actually performed a movement over the last `days`
    (1-730): sets/reps/kg per session, newest first. Fuzzy-matched against
    logged Garmin sets — "goblet squat" finds GOBLET_SQUAT. Check this
    before prescribing a load for a movement."""
    from jim.tools.history import exercise_history

    if not exercise.strip():
        raise ToolError("exercise is empty — name the movement to look up")
    days = _bounded(days, "days", 1, 730)
    user_id = _current_user_id()
    _ensure_history(user_id)
    return exercise_history(user_id, exercise, days=days)


@mcp.tool
def get_recent_activities(days: int = 14) -> str:
    """Recent Garmin activities (type, duration) over the last `days`
    (1-90), plan adherence where recorded, plus a daily step-count line per
    day — general daily-activity context, not structured training. If the
    step fetch fails, the activity list still comes back with a note."""
    from jim.tools.garmin import get_daily_steps
    from jim.tools.history import workout_history

    days = _bounded(days, "days", 1, 90)
    user_id = _current_user_id()
    _ensure_history(user_id)
    text = workout_history(user_id, days=days)

    today = _user_today(user_id)
    steps = _best_effort(
        user_id, "daily steps", get_daily_steps, today - timedelta(days=days), today,
    )
    if isinstance(steps, dict):
        text = f"{text}\n\nDaily steps: unavailable ({steps['unavailable']})"
    elif steps:
        step_lines = "\n".join(
            f"{s.get('calendarDate')}: {s.get('totalSteps')} steps"
            f" (goal {s.get('stepGoal')})"
            for s in steps
        )
        text = f"{text}\n\nDaily steps:\n{step_lines}"
    return text


@mcp.tool
def get_scheduled_workouts(start: str, end: str) -> list[dict]:
    """Workouts actually on the Garmin calendar between `start` and `end`
    (ISO dates, inclusive, at most 92 days apart) — what's really
    scheduled, not what Jim thinks it pushed. Completed sessions drop off
    Garmin's calendar, so this shows planned-not-yet-done items."""
    from jim.tools.garmin import get_scheduled_workouts as _get

    start_d, end_d = _parse_date(start, "start"), _parse_date(end, "end")
    if end_d < start_d:
        raise ToolError(f"end ({end}) is before start ({start})")
    if (end_d - start_d).days > 92:
        raise ToolError("range is over 92 days — split it into smaller windows")
    user_id = _current_user_id()
    with _garmin(user_id, "read the Garmin calendar"):
        rows = _get(user_id, start_d, end_d)
    return [{**r, "date": r["date"].isoformat()} for r in rows]


@mcp.tool
def list_saved_workouts() -> list[dict]:
    """The athlete's Garmin workout library: workout_id, name, sport. Names
    starting "Jim · " are one-off sessions from create_or_update_workout
    (swept after their date); everything else is a permanent workout.
    Build new ones with save_to_library, edit with update_workout."""
    from jim.tools.garmin import list_garmin_workouts

    user_id = _current_user_id()
    with _garmin(user_id, "list saved workouts"):
        return list_garmin_workouts(user_id)


def _prune(value: Any) -> Any:
    """Drop null/empty fields from Garmin's workout JSON — most of each step
    is nulls, which is noise for the model to read through."""
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


@mcp.tool
def get_saved_workout(workout_id: str) -> dict:
    """Full step-by-step detail for one Garmin workout (null fields
    stripped): name, sport, description, and every step with its end
    condition, targets, and nested repeat groups. Read this before
    update_workout so you can resend the full workout with just your change."""
    from jim.tools.garmin import get_garmin_workout_detail

    wid = _parse_workout_id(workout_id)
    user_id = _current_user_id()
    with _garmin(user_id, f"read workout {wid}"):
        return _prune(get_garmin_workout_detail(user_id, wid))



def _garmin_exercise_key(name: str) -> str | None:
    """The Garmin exerciseName a planned step maps to (the key logged sets
    use), so planned reps can be matched to what was actually lifted."""
    import re

    from jim.tools.garmin import classify_garmin_exercise

    if re.fullmatch(r"[A-Z0-9_]+", name):
        return name
    return classify_garmin_exercise(name)[1]


@mcp.tool
def get_week_overview() -> dict:
    """One read for a weekly check-in or planning next week — everything a
    coaching turn otherwise gathers with 4-5 calls, plus load suggestions:

    - `readiness`: today's verdict (and a recovery_note if last night's
      sleep/HRV hasn't synced yet)
    - `constraints`: the athlete's limits — apply them to everything below
    - `last_7_days`: recorded activities, and every planned session with
      its status: done, did_something_else (moved, but not what was
      planned), missed, or pending (today, nothing recorded yet)
    - `next_7_days`: what's on the Garmin calendar
    - `progression`: per exercise trained in the last 3 weeks, its recent
      history and a suggested next load/reps — increase, add_reps, hold,
      deload, add_time, or harder_variation — in the athlete's own unit
      (lb or kg, detected from how they load it), with the reason
    - `suggested_changes`: plain-language flags for next week (stalled
      lifts, missed sessions, load trend, muscle groups gone quiet)

    These are rule-based suggestions, not decisions. Before proposing any
    of it: check each against `constraints` (especially knee/ankle-loaded
    legs work — a "deload" or "hold" there may be deliberate), remember
    rep counts come from the watch and are often off by a few, and show the
    athlete a draft. Apply only on an explicit yes — update_workout for a
    library workout's new weights, create_or_update_workout for a one-off.
    When the week's load is high, increases are already turned into holds."""
    from jim.jobs.reconcile import adhered
    from jim.schemas import ActivitySummary
    from jim.tools.garmin import KIND_BY_SPORT_KEY, list_garmin_workouts
    from jim.tools.garmin import get_scheduled_workouts as calendar_between
    from jim.tools.history import activities_between, exercise_sets_since, readiness_read
    from jim.tools.progression import progression_report, workout_changes

    user_id = _current_user_id()
    _ensure_history(user_id)
    today = _user_today(user_id)
    week_ago, week_ahead = today - timedelta(days=7), today + timedelta(days=7)

    readiness = readiness_read(user_id, today).model_dump(mode="json")
    if all(readiness.get(k) is None for k in ("body_battery", "hrv", "sleep_hours")):
        readiness["recovery_note"] = (
            "last night's sleep/HRV hasn't synced yet — this verdict is load-only"
        )

    activities = activities_between(user_id, week_ago, today)
    calendar = _best_effort(user_id, "calendar", calendar_between, week_ago, week_ahead)
    library = _best_effort(user_id, "workout library", list_garmin_workouts)
    sport_by_id = (
        {w["workout_id"]: w["sport"] for w in library} if isinstance(library, list) else {}
    )

    # Planned sessions: Jim's own records, plus anything else on the Garmin
    # calendar (scheduled by hand in Garmin Connect). Completed workouts drop
    # off Garmin's calendar, so for past days Jim's records are the fuller
    # source; a past item still on the calendar wasn't run from the watch.
    plans: dict[tuple[str, str], dict[str, Any]] = {}
    for row in memory.planned_between(user_id, week_ago, week_ahead):
        plan = row["plan"]
        plans[(row["for_date"].isoformat(), row["workout_id"] or f"s{row['id']}")] = {
            "date": row["for_date"].isoformat(), "title": plan.get("title", ""),
            "kind": plan.get("kind", "other"), "workout_id": row["workout_id"],
        }
    on_calendar: set[tuple[str, str]] = set()
    if isinstance(calendar, list):
        for item in calendar:
            key = (item["date"].isoformat(), item["workout_id"])
            on_calendar.add(key)
            plans.setdefault(key, {
                "date": key[0], "title": item["title"], "workout_id": item["workout_id"],
                "kind": KIND_BY_SPORT_KEY.get(sport_by_id.get(item["workout_id"], ""), "other"),
            })

    by_day: dict[str, list[ActivitySummary]] = {}
    for a in activities:
        by_day.setdefault(a["day"].isoformat(), []).append(ActivitySummary(
            activity_id=str(a["activity_id"]), type=a["type"] or "unknown",
            duration_min=float(a["duration_min"] or 0),
        ))

    reviewed, upcoming = [], []
    for key in sorted(plans):
        p = plans[key]
        if p["date"] > today.isoformat():
            if not isinstance(calendar, list) or key in on_calendar:
                upcoming.append(p)
            continue
        actuals = by_day.get(p["date"], [])
        kind = p["kind"] if p["kind"] in _VALID_KINDS else "other"
        ok, note = adhered(
            StructuredSession(for_date=date.fromisoformat(p["date"]), kind=kind,
                              title=p["title"]),
            actuals,
        )
        if ok:
            status = "done"
        elif p["date"] == today.isoformat() and not actuals:
            status = "pending"
        elif actuals:
            status = "did_something_else"
        else:
            status = "missed"
        reviewed.append({**p, "status": status, "note": note,
                         **({"still_on_calendar": True} if key in on_calendar else {})})

    counted = [r for r in reviewed if r["status"] != "pending"]
    done = sum(r["status"] == "done" for r in counted)

    sets = exercise_sets_since(user_id, today - timedelta(days=56))
    planned_reps: dict[str, int] = {}
    for row in memory.planned_between(user_id, today - timedelta(days=60), week_ahead):
        for step in row["plan"].get("steps") or []:
            name = _garmin_exercise_key(step.get("exercise") or "")
            if name and step.get("reps"):
                planned_reps[name] = int(step["reps"])

    acwr = readiness.get("acwr")
    hold = None
    if readiness.get("status") in ("ease", "rest"):
        hold = f"readiness says \"{readiness.get('headline')}\""
    elif acwr is not None and acwr > 1.3:
        hold = f"load is high this week (ACWR {acwr})"
    progression = progression_report(sets, planned_reps, today, hold)

    return {
        "as_of": today.isoformat(),
        "readiness": {k: readiness.get(k) for k in (
            "status", "headline", "detail", "acwr", "body_battery", "hrv",
            "sleep_hours", "recovery_note") if readiness.get(k) is not None},
        "constraints": db.get_constraints(user_id) or "(none recorded — ask before loading legs)",
        "last_7_days": {
            "activities": [
                {"date": a["day"].isoformat(), "type": a["type"],
                 "minutes": round(float(a["duration_min"] or 0))}
                for a in activities
            ],
            "planned_vs_done": reviewed,
            "summary": (f"{done} of {len(counted)} planned sessions done as planned"
                        if counted else "no planned sessions on record for the last 7 days"),
        },
        "next_7_days": upcoming if isinstance(calendar, list) else {
            "unavailable": calendar.get("unavailable") if isinstance(calendar, dict) else None,
            "from_jims_records": upcoming,
        },
        "progression": progression,
        "suggested_changes": workout_changes(progression, reviewed, sets, readiness, today),
    }


# --- write: create/schedule/unschedule ---------------------------------------


@mcp.tool
def create_or_update_workout(
    for_date: str, title: str, kind: str, steps: list[StepIn], notes: str = "",
) -> dict:
    """Create a new Garmin workout from structured steps (exercise, sets,
    reps or duration_sec, weight_kg) and return its `workout_id`. To change
    a workout that already exists (this one's output, or any other),
    prefer `update_workout` — it edits in place by id rather than creating
    a new one. Only fall back to re-scheduling a fresh version over the old
    one if `update_workout` itself errors on a real account.

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

    For a superset (two or more exercises done back-to-back as one round,
    repeated — e.g. "3 rounds of wall sit then row"), give each step the
    same `superset_group` integer and the same `sets` value; they must be
    consecutive in `steps`. That's the only way to express it — Garmin's
    repeat block wraps one exercise by default (steps without a
    superset_group each get their own individual repeat when sets>1, same
    as always), and a shared superset_group is what tells it to wrap
    multiple exercises under one shared round count instead.

    For a genuinely nested structure — "for 3 total rounds: 2x8 squats,
    then 12 lunges," where squats need their own repeat count INSIDE each
    of the 3 outer rounds — superset_group alone can't express it (it only
    wraps once, at one level). Give the steps in that block the same
    `pyramid_group` integer and the same `pyramid_rounds`, still
    consecutive. It composes with superset_group: if the inner block is
    itself a superset, set both on those steps. Most sessions don't need
    this — reach for it only when a block is genuinely nested, not for an
    ordinary superset (that's superset_group alone).

    For a rest step the athlete advances past with a tap rather than a
    fixed timer, set `self_paced_rest=True` on that step — Garmin's actual
    press-to-continue rest type, not a timed interval standing in for one.
    reps/duration_sec/weight_kg are ignored on that step. Works inside a
    superset_group too (e.g. exercise, exercise, self-paced rest, all
    sharing one round).

    Other per-step options, all optional:
    - `role`: "warmup", "cooldown", "recovery", "other", or "main" instead
      of the default "interval" — shows correctly on the watch as that real
      step type rather than a generic interval. Use for an actual
      warmup/cooldown block, not just a step you happen to put first/last.
    - `distance_m`: end the step on distance (meters) instead of reps or
      duration_sec — e.g. "run 5km." Only one of reps/distance_m/
      end_at_heart_rate_bpm/duration_sec is used, in that priority order.
    - `end_at_heart_rate_bpm`: end the step once heart rate reaches this
      bpm instead of a fixed time — "recover until HR drops to 130,"
      typically with `role="recovery"`.
    - `target_heart_rate_zone` / `target_power_zone`: the athlete's own
      Garmin zone number (typically 1-5) for this step — "Zone 2 for 20
      min." Set at most one; heart rate wins if both are set.
    - `target_pace_min_mps` / `target_pace_max_mps`: a pace target as a
      speed range in meters/second — "5x400m @ 5k pace." Ask the athlete
      to confirm this displays as the pace they expect the first time you
      use it; the unit convention here wasn't independently verified
      against the watch display, only that Garmin accepts it.
    - `secondary_target_cadence_min` / `_max`: a second target riding
      alongside `target_heart_rate_zone`/`target_power_zone` — e.g. HR
      zone as primary, cadence range on top. No effect without a primary
      target also set on the same step.

    `notes` is a workout-level note visible on the workout itself, separate
    from `title` — e.g. "scaled down from last week, elbow's still sore."

    The title is auto-prefixed ("Jim · ...") so this one-off adaptation is
    distinguishable from the athlete's real saved workouts (Full Body A,
    PT Day, etc.) and gets swept automatically once its date has passed —
    see jobs/nightly.py's cleanup_adapted_workouts. Don't use this for a
    workout meant to stick around in the athlete's library; that's a
    library edit on Garmin itself (create_or_update_workout is for a
    single day's session, not a template).

    The workout is created AND scheduled on `for_date` in one call — no
    separate `schedule_workout` needed; calling it again would put a second
    copy on the calendar. If the scheduling half fails, the just-created
    workout is deleted again so no unscheduled orphan is left behind, and
    the error says so."""
    from jim.tools.garmin import create_garmin_workout, delete_garmin_workout
    from jim.tools.garmin import schedule_workout as schedule_garmin_workout

    day = _parse_date(for_date, "for_date")
    normalized_kind = _normalize_kind(kind)
    user_id = _current_user_id()
    title = title.strip()
    if title.startswith(ADAPTED_WORKOUT_PREFIX):
        title = title[len(ADAPTED_WORKOUT_PREFIX):]
    if not title:
        raise ToolError("title is empty")
    session = StructuredSession(
        for_date=day,
        kind=normalized_kind,
        title=f"{ADAPTED_WORKOUT_PREFIX}{title}",
        steps=_to_steps(steps, normalized_kind),
        rationale_summary=notes,
    )
    with _garmin(user_id, "create the workout"):
        ref = create_garmin_workout(user_id, session)
    try:
        with _garmin(user_id, f"schedule workout {ref.workout_id} on {day}"):
            schedule_garmin_workout(user_id, ref.workout_id, day)
    except ToolError as e:
        try:
            delete_garmin_workout(user_id, ref.workout_id)
            cleanup = "the unscheduled workout was deleted again, so nothing changed"
        except Exception:  # noqa: BLE001
            cleanup = (
                f"workout {ref.workout_id} was created but is NOT on the calendar —"
                " retry schedule_workout with that id, or delete_workout it"
            )
        raise ToolError(f"{e}; {cleanup}") from e
    _remember("create", memory.record_plan, user_id, ref.workout_id, session)
    return {**ref.model_dump(mode="json"), "title": session.title, "scheduled_for": day.isoformat()}


@mcp.tool
def save_to_library(title: str, kind: str, steps: list[StepIn], notes: str = "") -> dict:
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

    Garmin has no OFFICIALLY documented in-place edit for a saved workout,
    but `update_workout` reaches one anyway (see its docstring) — try that
    first when the athlete wants to change something that already exists.
    Only fall back to create-new/repoint/delete-old if `update_workout`
    itself errors on a real account.

    Same `kind`/step rules as create_or_update_workout (see its docstring
    for the full list, the strength/mobility-only exercise matching, and
    what `notes` does). The title can't start with "Jim · " — that prefix
    marks a workout for automatic cleanup, the opposite of permanent."""
    from jim.tools.garmin import create_garmin_workout

    title = title.strip()
    if not title:
        raise ToolError("title is empty")
    if title.startswith(ADAPTED_WORKOUT_PREFIX):
        raise ToolError(
            f"a permanent workout can't be titled {ADAPTED_WORKOUT_PREFIX!r}... — that"
            " prefix marks one-offs for automatic deletion; drop it"
        )
    normalized_kind = _normalize_kind(kind)
    user_id = _current_user_id()
    session = StructuredSession(
        for_date=_user_today(user_id),
        kind=normalized_kind,
        title=title,
        steps=_to_steps(steps, normalized_kind),
        rationale_summary=notes,
    )
    with _garmin(user_id, "save the workout to the library"):
        ref = create_garmin_workout(user_id, session)
    return {**ref.model_dump(mode="json"), "title": title}


@mcp.tool
def update_workout(
    workout_id: str, title: str, kind: str, steps: list[StepIn], notes: str = "",
) -> dict:
    """Update an EXISTING Garmin workout (by id) IN PLACE — same workout_id
    after the call, watch/library entry updated rather than replaced. This
    works for anything with a workout_id: a permanent library entry from
    `save_to_library`, or a one-off from `create_or_update_workout`.

    Uses Garmin's per-id update endpoint (a PUT, the same thing Garmin
    Connect's website does when you edit a saved workout). Undocumented,
    but live-verified: same workout_id afterward, everything replaced, no
    duplicate left behind. Scheduled days keep pointing at it, so there's
    nothing to reschedule. If this ever errors, fall back to: call
    `save_to_library`/`create_or_update_workout` again with the corrected
    steps (a new workout_id comes back), repoint any scheduled days at the
    new id via `schedule_workout`, then `delete_workout` the old id once
    confirmed — don't delete first.

    You must pass the FULL desired workout every time — `title`, `kind`,
    and the complete `steps` list — the same as creating one; there's no
    partial/merge update. Same `kind`/step rules as create_or_update_workout
    (see its docstring). Whatever prefix or lack of one the workout already
    had (the "Jim · " one-off prefix, or a permanent title) is preserved
    only if you pass the same `title` back — this tool does not add or
    strip that prefix itself, so re-send the title exactly as read from
    `list_saved_workouts`/`get_saved_workout` unless you're deliberately
    renaming it.

    Only call this on an explicit ask to change something that already
    exists — never silently, same as any other write. Read the current
    version with get_saved_workout first so nothing you didn't mean to
    change gets dropped."""
    from jim.tools.garmin import update_garmin_workout

    wid = _parse_workout_id(workout_id)
    title = title.strip()
    if not title:
        raise ToolError("title is empty — resend the workout's current title to keep it")
    normalized_kind = _normalize_kind(kind)
    user_id = _current_user_id()
    session = StructuredSession(
        for_date=_user_today(user_id),
        kind=normalized_kind,
        title=title,
        steps=_to_steps(steps, normalized_kind),
        rationale_summary=notes,
    )
    with _garmin(user_id, f"update workout {wid}"):
        ref = update_garmin_workout(user_id, wid, session)
    _remember("update", memory.update_plan, user_id, wid, session, session.for_date)
    return {**ref.model_dump(mode="json"), "title": title}


@mcp.tool
def schedule_workout(workout_id: str, on: str) -> dict:
    """Put an existing Garmin workout (by id) on the calendar for `on` (ISO
    date). Use for library workouts (Full Body A, PT Day, ...) — one-offs
    from create_or_update_workout are already scheduled, and scheduling one
    again adds a second copy. Scheduling doesn't replace anything already on
    that day; unschedule_day first if the day should only have this one.
    Only call on an explicit ask to push/schedule, never as a side effect of
    discussing a plan."""
    from jim.tools.garmin import schedule_workout as _schedule

    wid = _parse_workout_id(workout_id)
    day = _parse_date(on, "on")
    user_id = _current_user_id()
    with _garmin(user_id, f"schedule workout {wid} on {day}"):
        _schedule(user_id, wid, day)
    _remember("schedule", _record_scheduled, user_id, wid, day)
    return {"ok": True, "workout_id": wid, "scheduled_for": day.isoformat()}


@mcp.tool
def unschedule_day(on: str, workout_id: str | None = None) -> dict:
    """Remove planned (not completed) workouts from the calendar on `on`
    (ISO date) — all of them, or only the one matching `workout_id`. The
    workouts stay in the library; only the calendar entry goes. Returns
    what was removed (an empty list means nothing matched). Use before
    putting a replacement on a day so it doesn't end up with two."""
    from jim.tools.garmin import clear_schedule

    day = _parse_date(on, "on")
    wid = _parse_workout_id(workout_id) if workout_id is not None else None
    user_id = _current_user_id()
    with _garmin(user_id, f"clear the calendar on {day}"):
        removed = clear_schedule(user_id, day, wid)
    _remember("unschedule", memory.cancel_plans, user_id,
              [r["workout_id"] for r in removed], on=day)
    return {"ok": True, "date": day.isoformat(), "removed": removed}


@mcp.tool
def delete_workout(workout_id: str) -> dict:
    """Permanently delete a Garmin workout from the library (and with it any
    calendar entries). Can't be undone. For a one-off that's no longer
    wanted this is routine; for a permanent library workout (no "Jim · "
    prefix) only do it on an explicit ask to delete that specific workout —
    to take it off one day instead, use unschedule_day."""
    from jim.tools.garmin import delete_garmin_workout

    wid = _parse_workout_id(workout_id)
    user_id = _current_user_id()
    with _garmin(user_id, f"delete workout {wid}"):
        delete_garmin_workout(user_id, wid)
    _remember("delete", memory.cancel_plans, user_id, [wid], from_day=_user_today(user_id))
    return {"ok": True, "deleted": wid}


@mcp.tool
def backfill_history(days: int = 90) -> dict:
    """Force a re-pull of the trailing `days` of Garmin history (daily
    metrics, activities, exercise sets) into Jim's database, even if some
    history already exists. Every other read tool auto-backfills once on an
    account's first-ever call, but only when it has zero history — an
    account that connected Garmin before that existed (or only ever synced
    a few days) won't get topped up automatically. Call this on an explicit
    ask like "backfill my history" or "pull in my past workouts." Runs
    synchronously and makes one Garmin round trip per day (0-120 days;
    0 re-syncs just today), so ~90 days takes a couple of minutes — say so
    before calling it. Safe to repeat: existing rows are updated, not
    duplicated."""
    from jim.tools.garmin import backfill_history as _backfill

    days = _bounded(days, "days", 0, 120)
    user_id = _current_user_id()
    with _garmin(user_id, "backfill history"):
        _backfill(user_id, _user_today(user_id), days)
    return {"ok": True, "days": days}


@mcp.tool
def cleanup_old_adapted_workouts(lookback_days: int = 30) -> dict:
    """Delete past one-off workouts this server created (titled "Jim · ...")
    so they don't pile up in the athlete's Garmin library and watch. Runs
    automatically every night; call it directly if asked to "clean up" now.
    Only touches one-offs created between `lookback_days` (1-365) ago and
    yesterday — today's are never swept, and permanent library workouts
    (no prefix) never are. Returns how many one-offs remain afterward."""
    from jim.jobs.nightly import cleanup_adapted_workouts
    from jim.tools.garmin import list_garmin_workouts

    lookback_days = _bounded(lookback_days, "lookback_days", 1, 365)
    user_id = _current_user_id()
    with _garmin(user_id, "clean up old one-off workouts"):
        cleanup_adapted_workouts(user_id, _user_today(user_id), lookback_days)
        remaining = [
            w["name"] for w in list_garmin_workouts(user_id)
            if w["name"].startswith(ADAPTED_WORKOUT_PREFIX)
        ]
    return {"ok": True, "one_offs_remaining": remaining}


# --- constraints: the one remaining piece of Jim-side memory -----------------


@mcp.tool
def get_constraints() -> str:
    """The athlete's standing limits (injuries, knee/ankle/wrist rules),
    safety rules, and goals, as free text. Read this before proposing any
    session — it's the safety authority now that there's no code-enforced
    guardrail. An empty string means nothing's recorded yet, not that there
    are no limits — ask."""
    return db.get_constraints(_current_user_id())


@mcp.tool
def set_constraints(content: str, allow_empty: bool = False) -> dict:
    """REPLACE the athlete's whole constraints doc with `content` — not a
    merge. Always get_constraints first and send back the existing text with
    your change folded in, or everything else in it is lost. Call only when
    the athlete states a new limit, rule, or goal (or asks to change one).
    Sending empty content is refused unless `allow_empty=True`, which should
    only be used on an explicit ask to wipe everything."""
    if not content.strip() and not allow_empty:
        raise ToolError(
            "refusing to overwrite the constraints doc with empty text — that would"
            " erase every recorded limit. Pass allow_empty=True only if the athlete"
            " explicitly asked to clear it."
        )
    user_id = _current_user_id()
    db.set_constraints(user_id, content)
    return {"ok": True, "length": len(content)}


def build_asgi_app():
    """Mounted at /mcp in app.py (`app.mount("/mcp", ...)`), so this app's own
    internal path is left at the root — mounting handles where it's exposed.
    `stateless_http=True` is load-bearing — see the module docstring."""
    return mcp.http_app(path="/", stateless_http=True)
