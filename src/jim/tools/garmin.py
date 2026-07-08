"""Garmin tools: read today's state, create + schedule structured workouts.

Auth is mobile-SSO via `python-garminconnect`; tokens cache at ~/.garminconnect
and MFA may be prompted on first/expired login. Never hardcode credentials.

The write path is the workout API (JSON) — FIT structured-workout upload is
rejected (406). The exact strength JSON accepted by the account is the M1
unknown; `build_strength_payload` is the current best guess and the accepted
shape must be documented in docs/garmin_strength.md once the M1 round-trip
passes on a real device."""

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from jim.config import settings
from jim.schemas import ActivitySummary, GarminToday, StructuredSession, WorkoutRef

if TYPE_CHECKING:
    from jim.playbook import WorkoutTemplate

log = logging.getLogger(__name__)

_client: Any = None


TOKEN_STORE = "~/.garminconnect"


def client() -> Any:
    """Lazily authenticated Garmin client (cached tokens, re-login on expiry)."""
    global _client
    if _client is None:
        from garminconnect import Garmin

        cfg = settings()
        garmin = Garmin(cfg.garmin_email, cfg.garmin_password)
        # Loads cached tokens from TOKEN_STORE when present; otherwise does a
        # full SSO login and dumps fresh tokens there.
        garmin.login(TOKEN_STORE)
        _client = garmin
    return _client


def get_garmin_today(day: date) -> GarminToday:
    """Activities + recovery for `day`. Computation done here; returns summary."""
    api = client()
    iso = day.isoformat()

    activities = []
    for raw in api.get_activities_by_date(iso, iso) or []:
        activities.append(
            ActivitySummary(
                activity_id=str(raw.get("activityId", "")),
                type=str(raw.get("activityType", {}).get("typeKey", "unknown")),
                duration_min=round(float(raw.get("duration") or 0) / 60, 1),
                training_load=raw.get("activityTrainingLoad"),
            )
        )

    stats = api.get_stats(iso) or {}
    sleep = (api.get_sleep_data(iso) or {}).get("dailySleepDTO") or {}
    hrv = ((api.get_hrv_data(iso) or {}).get("hrvSummary") or {}).get("lastNightAvg")

    sleep_sec = sleep.get("sleepTimeSeconds")
    return GarminToday(
        day=day,
        activities=activities,
        hrv=hrv,
        sleep_hours=round(sleep_sec / 3600, 1) if sleep_sec else None,
        body_battery=stats.get("bodyBatteryMostRecentValue"),
        readiness=stats.get("trainingReadinessScore"),
        resting_hr=stats.get("restingHeartRate"),
    )


# Garmin's fixed exercise taxonomy (FIT SDK enums). Substring -> (category,
# exerciseName). Unmapped movements omit category and rely on the description;
# free-text categories are rejected with "Invalid category". Order matters:
# first match wins, so specific entries go before generic ones.
# Every (category, exerciseName) pair below is verified against Garmin's own
# taxonomy (connect.garmin.com/web-data/exercises/Exercises.json) so steps
# render with a proper exercise name/animation on-watch instead of falling
# back to the free-text description. First match wins: specific before generic.
# Some PT movements have no Garmin equivalent (e.g. ankle eversion) — the
# nearest-name enum or a bare category is used so the step isn't "unknown".
EXERCISE_TAXONOMY: tuple[tuple[str, str, str | None], ...] = (
    # squat family
    ("goblet squat", "SQUAT", "GOBLET_SQUAT"),
    ("balancing squat", "SQUAT", "BALANCING_SQUAT"),
    ("wall sit", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    ("wall squat", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    ("spanish squat", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),  # closest Garmin has
    ("step-down", "SQUAT", "STEP_UP"),  # eccentric emphasis noted in description
    ("step down", "SQUAT", "STEP_UP"),
    ("step-up", "SQUAT", "DUMBBELL_STEP_UP"),
    ("step up", "SQUAT", "DUMBBELL_STEP_UP"),
    ("squat", "SQUAT", None),
    # hinge / posterior chain
    ("romanian deadlift", "DEADLIFT", "ROMANIAN_DEADLIFT"),
    ("deadlift", "DEADLIFT", None),
    ("back extension", "BANDED_EXERCISES", "BACK_EXTENSION"),
    ("good morning", "LEG_CURL", "GOOD_MORNING"),
    # quad / knee rehab
    ("terminal knee extension", "BANDED_EXERCISES", "LEG_EXTENSION"),
    ("short arc quad", "CRUNCH", "LEG_EXTENSIONS"),  # account precedent for iso holds
    ("straight-leg raise", "LEG_RAISE", "LYING_STRAIGHT_LEG_RAISE"),
    ("straight leg raise", "LEG_RAISE", "LYING_STRAIGHT_LEG_RAISE"),
    # hip
    ("single-leg bridge", "HIP_RAISE", "SINGLE_LEG_HIP_RAISE"),
    ("single leg bridge", "HIP_RAISE", "SINGLE_LEG_HIP_RAISE"),
    ("glute bridge", "BANDED_EXERCISES", "GLUTE_BRIDGE"),
    ("clamshell", "BANDED_EXERCISES", "CLAM_SHELLS"),
    ("clam shell", "BANDED_EXERCISES", "CLAM_SHELLS"),
    ("lateral band walk", "BANDED_EXERCISES", "LATERAL_BAND_WALKS"),
    ("hip controlled articular", "HIP_STABILITY", "HIP_CIRCLES"),
    ("hip circles", "HIP_STABILITY", "HIP_CIRCLES"),
    ("dead bug", "HIP_STABILITY", "DEAD_BUG"),
    ("prone hip internal rotation", "HIP_STABILITY", "PRONE_HIP_INTERNAL_ROTATION"),
    ("single-leg circles", "CORE", "SINGLE_LEG_CIRCLES"),
    ("single leg circles", "CORE", "SINGLE_LEG_CIRCLES"),
    ("single-leg reach", "HIP_STABILITY", None),
    ("single leg reach", "HIP_STABILITY", None),
    # ankle / calf
    ("seated toe raise", "CALF_RAISE", "SEATED_DUMBBELL_TOE_RAISE"),
    ("dorsiflexion", "WARM_UP", "ANKLE_DORSIFLEXION_WITH_BAND"),
    ("eccentric calf raise", "CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
    ("single-leg calf raise", "CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
    ("calf raise", "CALF_RAISE", None),
    ("eversion", "CALF_RAISE", None),  # Garmin has no eversion; keep the calf/ankle icon
    ("ankle circles", "WARM_UP", "ANKLE_CIRCLES"),
    ("seated marching", "WARM_UP", "ANKLE_CIRCLES"),
    # stretches / cardio (before the generic "row" needle — "rower" contains it)
    ("hip flexor stretch", "WARM_UP", "STRETCH_LUNGING_HIP_FLEXOR"),
    ("bike", "CARDIO", None),
    ("rower", "CARDIO", None),
    ("cardio", "CARDIO", None),
    # core / upper
    ("side plank", "PLANK", "SIDE_PLANK"),
    ("plank", "PLANK", None),
    ("lunge", "LUNGE", None),
    ("bench press", "BENCH_PRESS", None),
    ("row", "ROW", None),
    ("curl", "CURL", None),
    ("pull-up", "PULL_UP", None),
    ("pull up", "PULL_UP", None),
    ("push-up", "PUSH_UP", None),
    ("push up", "PUSH_UP", None),
    ("quad sets", "CRUNCH", "LEG_EXTENSIONS"),
)


def classify_garmin_exercise(name: str) -> tuple[str | None, str | None]:
    lowered = name.lower()
    for needle, category, exercise_name in EXERCISE_TAXONOMY:
        if needle in lowered:
            return category, exercise_name
    return None, None


SPORT_TYPES: dict[str, dict[str, Any]] = {
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "strength_training": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "mobility": {"sportTypeId": 11, "sportTypeKey": "mobility"},
    "conditioning": {"sportTypeId": 11, "sportTypeKey": "mobility"},
}


def _emit_step(
    order: int,
    *,
    name: str,
    sets: int,
    reps: int | None,
    time_sec: int | None,
    weight_kg: float | None,
) -> tuple[list[dict[str, Any]], int]:
    """Build one executable step (wrapped in a RepeatGroupDTO when sets>1),
    encoding the hard-won Garmin quirks (see docs/garmin_strength.md).

    Condition type IDs: 2 = time, 7 = iterations, 10 = reps — numeric id is
    mandatory; the value goes in step-level endConditionValue."""
    if reps:
        end_condition = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
        end_value: float = reps
    else:
        end_condition = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        end_value = time_sec or 60
    entry: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "description": name,
    }
    order += 1
    if weight_kg is not None:
        entry["weightValue"] = weight_kg
        entry["weightUnit"] = {"unitKey": "kilogram"}
    category, exercise_name = classify_garmin_exercise(name)
    if category:
        entry["category"] = category
    if exercise_name:
        entry["exerciseName"] = exercise_name

    if sets > 1:
        group = {
            "type": "RepeatGroupDTO",
            "stepOrder": entry["stepOrder"],
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "numberOfIterations": sets,
            "smartRepeat": False,
            "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
            "endConditionValue": sets,
            "workoutSteps": [{**entry, "stepOrder": order}],
        }
        order += 1
        return [group], order
    return [entry], order


def _wrap_payload(name: str, sport_key: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    sport = SPORT_TYPES.get(sport_key, SPORT_TYPES["strength"])
    return {
        "workoutName": name,
        "sportType": sport,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
        ],
    }


def build_strength_payload(session: StructuredSession) -> dict[str, Any]:
    """Garmin workout-API JSON for a composed session (verified schema)."""
    steps: list[dict[str, Any]] = []
    order = 1
    for step in session.steps:
        emitted, order = _emit_step(
            order,
            name=step.exercise,
            sets=step.sets,
            reps=step.reps,
            time_sec=step.duration_sec,
            weight_kg=step.weight_kg,
        )
        steps.extend(emitted)
    return _wrap_payload(session.title, "strength", steps)


def build_template_payload(template: "WorkoutTemplate") -> dict[str, Any]:
    """Garmin workout-API JSON for a playbook template (warmup + all blocks).

    Block-level `sets` (strength supersets) wrap the whole block in a repeat;
    exercise-level `sets` wrap a single move. Used to materialize PT/base
    routines that don't yet exist as Garmin workouts (e.g. home PT)."""
    steps: list[dict[str, Any]] = []
    order = 1
    for ex in template.warmup:
        emitted, order = _emit_step(
            order, name=ex.name, sets=ex.sets or 1, reps=ex.reps,
            time_sec=ex.time_sec, weight_kg=None,
        )
        steps.extend(emitted)
    for block in template.blocks:
        block_steps: list[dict[str, Any]] = []
        for ex in block.exercises:
            emitted, order = _emit_step(
                order, name=ex.name, sets=ex.sets or 1, reps=ex.reps,
                time_sec=ex.time_sec, weight_kg=None,
            )
            block_steps.extend(emitted)
        if block.sets and block.sets > 1:
            group = {
                "type": "RepeatGroupDTO",
                "stepOrder": block_steps[0]["stepOrder"],
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": block.sets,
                "smartRepeat": False,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                "endConditionValue": block.sets,
                "workoutSteps": block_steps,
            }
            steps.append(group)
        else:
            steps.extend(block_steps)
    return _wrap_payload(template.label, template.sport, steps)


def create_garmin_workout(session: StructuredSession) -> WorkoutRef:
    """Create a structured workout via the workout API (JSON path, NOT FIT upload)."""
    api = client()
    payload = build_strength_payload(session)
    resp = api.upload_workout(payload)
    workout_id = str(resp.get("workoutId", ""))
    log.info("created garmin workout %s (%s)", workout_id, session.title)
    return WorkoutRef(workout_id=workout_id)


def schedule_workout(workout_id: str, on: date) -> None:
    api = client()
    api.schedule_workout(workout_id, on.isoformat())
    log.info("scheduled workout %s for %s", workout_id, on)


def clear_schedule(on: date) -> None:
    """Unschedule every planned (not completed) workout on `on`.

    Used by the morning re-plan before pushing a replacement, so a stale
    nightly schedule doesn't sit next to the new one. Only touches calendar
    items of type 'workout' — recorded activities are untouched."""
    api = client()
    calendar = api.get_scheduled_workouts(on.year, on.month) or {}
    for item in calendar.get("calendarItems", []):
        if item.get("itemType") == "workout" and item.get("date") == on.isoformat():
            api.unschedule_workout(item["id"])
            log.info("unscheduled stale workout %s (%s) on %s", item.get("id"),
                     item.get("title"), on)
