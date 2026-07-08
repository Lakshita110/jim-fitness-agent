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
        session([ExerciseStep(exercise="Nordic hamstring thing", sets=1, reps=5)])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert "category" not in step
    assert step["description"] == "Nordic hamstring thing"


def test_taxonomy_prefers_specific_match():
    assert classify_garmin_exercise("Goblet Squat") == ("SQUAT", "GOBLET_SQUAT")
    assert classify_garmin_exercise("Back squat") == ("SQUAT", None)
    assert classify_garmin_exercise("Romanian Deadlift") == ("DEADLIFT", "ROMANIAN_DEADLIFT")
    assert classify_garmin_exercise("mystery move") == (None, None)


def test_build_template_payload_from_playbook_home_pt():
    from jim.playbook import load_playbook
    from jim.tools.garmin import build_template_payload

    home = load_playbook().pt_routines["pt_home"]
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
