"""Payload shaping against the VERIFIED Garmin schema (docs/garmin_strength.md).
Live calls are exercised by scripts/, not CI."""

from datetime import date

from jim.schemas import ExerciseStep, StructuredSession
from jim.tools.garmin import build_strength_payload, classify_garmin_exercise


def session(steps: list[ExerciseStep]) -> StructuredSession:
    return StructuredSession(
        for_date=date(2026, 7, 8),
        kind="strength",
        title="Test session",
        steps=steps,
        est_duration_min=30,
    )


def test_multi_set_step_becomes_repeat_group():
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Goblet squat", sets=3, reps=8, weight_kg=16)])
    )
    assert payload["sportType"]["sportTypeKey"] == "strength_training"
    (group,) = payload["workoutSegments"][0]["workoutSteps"]
    assert group["type"] == "RepeatGroupDTO"
    assert group["numberOfIterations"] == 3
    assert group["endCondition"] == {"conditionTypeId": 7, "conditionTypeKey": "iterations"}

    (exercise,) = group["workoutSteps"]
    assert exercise["type"] == "ExecutableStepDTO"
    assert exercise["endCondition"] == {"conditionTypeId": 10, "conditionTypeKey": "reps"}
    assert exercise["endConditionValue"] == 8  # step-level, NOT inside endCondition
    assert exercise["weightValue"] == 16
    assert exercise["weightUnit"] == {"unitKey": "kilogram"}
    assert exercise["category"] == "SQUAT"
    assert exercise["exerciseName"] == "GOBLET_SQUAT"


def test_single_set_timed_step_stays_flat():
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Side plank", sets=1, duration_sec=40)])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["type"] == "ExecutableStepDTO"
    assert step["endCondition"] == {"conditionTypeId": 2, "conditionTypeKey": "time"}
    assert step["endConditionValue"] == 40
    assert "weightValue" not in step  # no weight -> field omitted


def test_step_orders_are_sequential_across_groups():
    payload = build_strength_payload(
        session(
            [
                ExerciseStep(exercise="Goblet squat", sets=3, reps=8),
                ExerciseStep(exercise="Side plank", sets=1, duration_sec=40),
            ]
        )
    )
    top = payload["workoutSegments"][0]["workoutSteps"]
    assert top[0]["stepOrder"] == 1
    assert top[0]["workoutSteps"][0]["stepOrder"] == 2
    assert top[1]["stepOrder"] == 3


def test_unmapped_exercise_omits_category():
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Faff about a bit", sets=1, reps=5)])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert "category" not in step
    assert step["description"] == "Faff about a bit"


# --- matching a movement to Garmin's exercise library -------------------------


def test_matches_the_nearest_exercise_garmin_actually_has():
    # None of these are hand-mapped: they're found in the vendored library.
    assert classify_garmin_exercise("Goblet Squat") == ("SQUAT", "GOBLET_SQUAT")
    assert classify_garmin_exercise("Romanian Deadlift") == ("DEADLIFT", "ROMANIAN_DEADLIFT")
    assert classify_garmin_exercise("Lat pulldown") == ("PULL_UP", "LAT_PULLDOWN")
    assert classify_garmin_exercise("Bulgarian split squat")[1].endswith(
        "BULGARIAN_SPLIT_SQUAT"
    )
    # a movement with no close equivalent stays unmapped rather than guessing
    assert classify_garmin_exercise("Faff about a bit") == (None, None)


def test_coaching_notes_do_not_hijack_the_match():
    """The exercise is the head of the name; "(3s lower)" or "— 60° hold" is a
    note. Left in, the last word is "lower"/"hold" and nothing matches."""
    assert classify_garmin_exercise("Hip flexor stretch (kneeling)") == (
        "WARM_UP", "STRETCH_LUNGING_HIP_FLEXOR",
    )
    assert classify_garmin_exercise("Leg extension — 60° isometric hold") == (
        "BANDED_EXERCISES", "LEG_EXTENSION",
    )
    assert classify_garmin_exercise("Seated bike, low resistance") == ("CARDIO", None)


def test_a_shared_prefix_is_not_a_match():
    """Without requiring the movement itself, "single-leg <anything>" all matched
    SINGLE_LEG_DIP on the strength of the two words they share."""
    assert classify_garmin_exercise("Single-leg bridge") == ("HIP_RAISE", "SINGLE_LEG_HIP_RAISE")
    assert classify_garmin_exercise("Single-leg circles") == ("HIP_STABILITY", "HIP_CIRCLES")


def test_compound_words_reach_the_words_garmin_spells_apart():
    assert classify_garmin_exercise("Resisted clamshell") == (
        "BANDED_EXERCISES", "CLAM_SHELLS",
    )


def test_overrides_beat_the_nearest_name_when_it_is_the_wrong_movement():
    # Garmin has no eversion at all, and its nearest name to a wall sit is a
    # WEIGHTED_WALL_SQUAT — a different exercise under load.
    assert classify_garmin_exercise("Wall sit (shallow, ~60°)") == (
        "SQUAT", "BODY_WEIGHT_WALL_SQUAT",
    )
    assert classify_garmin_exercise("Banded eversion") == ("CALF_RAISE", None)


def test_every_playbook_exercise_reaches_a_garmin_exercise():
    """The bug this guards: an unmatched movement lands on the watch as a bare
    note — no exercise, no animation, no set logging."""
    from jim.playbook import _load_playbook_from_disk

    playbook = _load_playbook_from_disk()
    templates = list(playbook.workouts.values()) + list(playbook.pt_routines.values())
    names = {
        exercise.name
        for template in templates
        for exercise in [*template.warmup, *(e for b in template.blocks for e in b.exercises)]
    }
    unmapped = [name for name in names if classify_garmin_exercise(name)[0] is None]
    assert not unmapped


def test_every_mapping_is_a_pair_garmin_will_accept():
    """category must be a real category and exerciseName one of ITS exercises —
    an invented pair is a live 400 ("Invalid category") or a silently dropped
    step. Guards the hand-written overrides against typos and drift."""
    from jim.tools.garmin import EXERCISE_OVERRIDES, exercise_library

    valid: dict[str, set[str]] = {}
    for category, exercise, _, _ in exercise_library():
        valid.setdefault(category, set()).add(exercise)

    for needle, category, exercise in EXERCISE_OVERRIDES:
        assert category in valid, f"{needle}: {category} is not a Garmin category"
        assert exercise is None or exercise in valid[category], (
            f"{needle}: {exercise} is not an exercise in {category}"
        )


def test_build_template_payload_from_playbook_home_pt():
    from jim.playbook import _load_playbook_from_disk
    from jim.tools.garmin import build_template_payload

    home = _load_playbook_from_disk().pt_routines["pt_home"]
    payload = build_template_payload(home)
    assert payload["sportType"]["sportTypeKey"] == "mobility"
    steps = payload["workoutSegments"][0]["workoutSteps"]
    # warmup (single, flat) + several exercises, multi-set ones wrapped in repeats
    assert any(s["type"] == "RepeatGroupDTO" for s in steps)
    # priority ankle eversion is present (by description) and repeated 3x
    everts = [
        s for s in steps
        if s.get("type") == "RepeatGroupDTO"
        and "eversion" in s["workoutSteps"][0]["description"].lower()
    ]
    assert everts and everts[0]["numberOfIterations"] == 3
