"""Payload shaping against the VERIFIED Garmin schema (docs/garmin_strength.md).
Live calls are exercised by scripts/, not CI."""

from datetime import date

from jim.schemas import ExerciseStep, StructuredSession
from jim.tools.garmin import build_strength_payload, classify_garmin_exercise


def session(steps: list[ExerciseStep], kind: str = "strength") -> StructuredSession:
    return StructuredSession(
        for_date=date(2026, 7, 8),
        kind=kind,
        title="Test session",
        steps=steps,
        est_duration_min=30,
    )


def test_conditioning_session_is_not_silently_tagged_strength():
    """Real bug: build_strength_payload used to hardcode sport_key="strength"
    regardless of session.kind, so a conditioning (walk/run/ride) session
    Claude asked for always got scheduled on Garmin as strength_training."""
    payload = build_strength_payload(session([ExerciseStep(exercise="Walk", duration_sec=1800)],
                                              kind="conditioning"))
    assert payload["sportType"]["sportTypeKey"] != "strength_training"


def test_conditioning_step_skips_strength_exercise_classification():
    """A walk isn't a movement in Garmin's strength exercise taxonomy —
    forcing it through classify_garmin_exercise attaches a wrong or empty
    category/exerciseName, which can get the step rejected. Conditioning
    steps should go out as plain description-only steps instead."""
    payload = build_strength_payload(session([ExerciseStep(exercise="Walk", duration_sec=1800)],
                                              kind="conditioning"))
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["description"] == "Walk"
    assert "category" not in step
    assert "exerciseName" not in step


def test_strength_session_still_gets_classified():
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Goblet squat", sets=1, reps=8)])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert "category" in step


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


def test_superset_wraps_multiple_exercises_in_one_repeat_group():
    """The reported bug: save_to_library had no way to group two different
    exercises under one shared round count — "Wall sit -> Row" x3 rounds
    just came out as 6 separate ungrouped steps. superset_group fixes that:
    ONE RepeatGroupDTO wrapping BOTH exercises, not two separate ones."""
    payload = build_strength_payload(
        session([
            ExerciseStep(exercise="Wall sit", sets=3, duration_sec=30, superset_group=1),
            ExerciseStep(exercise="Row", sets=3, reps=10, superset_group=1),
        ])
    )
    (group,) = payload["workoutSegments"][0]["workoutSteps"]
    assert group["type"] == "RepeatGroupDTO"
    assert group["numberOfIterations"] == 3
    wall_sit, row = group["workoutSteps"]
    assert wall_sit["description"] == "Wall sit"
    assert wall_sit["endConditionValue"] == 30
    assert row["description"] == "Row"
    assert row["endConditionValue"] == 10
    # both exercises get their own sequential stepOrder inside the group
    assert wall_sit["stepOrder"] < row["stepOrder"]


def test_self_paced_rest_uses_garmins_real_press_to_continue_step():
    """Previously a "rest" step was just a timed interval standing in for
    one (e.g. a fixed 20s timer) since there was no way to reach Garmin's
    actual press-to-continue rest type. self_paced_rest=True builds the
    real thing: stepTypeId 5 ("rest") + endCondition conditionTypeId 1
    ("lap.button") — live-verified against a real account, not documented
    anywhere. reps/duration/weight are irrelevant and should be ignored."""
    payload = build_strength_payload(
        session([
            ExerciseStep(exercise="Rest", duration_sec=20, self_paced_rest=True),
        ])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["stepType"] == {"stepTypeId": 5, "stepTypeKey": "rest"}
    assert step["endCondition"] == {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
    assert step["endConditionValue"] is None
    assert step["description"] == "Rest"
    assert "category" not in step
    assert "exerciseName" not in step


def test_self_paced_rest_works_inside_a_superset():
    """The exact reported use case: exercise, exercise, self-paced rest, all
    sharing one round via superset_group."""
    payload = build_strength_payload(
        session([
            ExerciseStep(exercise="Wall sit", sets=3, duration_sec=30, superset_group=1),
            ExerciseStep(exercise="Row", sets=3, reps=10, superset_group=1),
            ExerciseStep(exercise="Rest", sets=3, self_paced_rest=True, superset_group=1),
        ])
    )
    (group,) = payload["workoutSegments"][0]["workoutSteps"]
    assert group["type"] == "RepeatGroupDTO"
    assert len(group["workoutSteps"]) == 3
    rest_step = group["workoutSteps"][2]
    assert rest_step["stepType"] == {"stepTypeId": 5, "stepTypeKey": "rest"}
    assert rest_step["endCondition"]["conditionTypeKey"] == "lap.button"


def test_role_sets_the_real_stepType():
    """Every step defaulted to a generic "interval" stepType before role
    existed. Live-verified: warmup=1, cooldown=2, interval=3, recovery=4."""
    for role, expected_id in [("warmup", 1), ("cooldown", 2), ("recovery", 4)]:
        payload = build_strength_payload(
            session([ExerciseStep(exercise="Jog", duration_sec=300, role=role)],
                    kind="conditioning")
        )
        (step,) = payload["workoutSegments"][0]["workoutSteps"]
        assert step["stepType"]["stepTypeId"] == expected_id
        assert step["stepType"]["stepTypeKey"] == role


def test_role_defaults_to_interval():
    payload = build_strength_payload(session([ExerciseStep(exercise="Goblet squat", reps=8)]))
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["stepType"] == {"stepTypeId": 3, "stepTypeKey": "interval"}


def test_distance_m_becomes_a_distance_end_condition():
    """Live-verified: conditionTypeId 3, key "distance", value in meters."""
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Run", distance_m=5000)], kind="running")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["endCondition"] == {"conditionTypeId": 3, "conditionTypeKey": "distance"}
    assert step["endConditionValue"] == 5000


def test_reps_beats_distance_beats_duration_priority():
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Run", reps=10, distance_m=5000, duration_sec=300)])
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["endCondition"]["conditionTypeKey"] == "reps"

    payload = build_strength_payload(
        session([ExerciseStep(exercise="Run", distance_m=5000, duration_sec=300)],
                kind="running")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["endCondition"]["conditionTypeKey"] == "distance"


def test_heart_rate_zone_target():
    """Live-verified: workoutTargetTypeId 4, key "heart.rate.zone", plain zoneNumber."""
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Run", duration_sec=1200, target_heart_rate_zone=2)],
                kind="running")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["targetType"] == {
        "workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
    }
    assert step["zoneNumber"] == 2


def test_power_zone_target():
    """Live-verified: workoutTargetTypeId 2, key "power.zone", plain zoneNumber."""
    payload = build_strength_payload(
        session([ExerciseStep(exercise="Ride", duration_sec=1200, target_power_zone=3)],
                kind="cycling")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["targetType"] == {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}
    assert step["zoneNumber"] == 3


def test_heart_rate_zone_wins_over_power_zone_when_both_set():
    payload = build_strength_payload(
        session([ExerciseStep(
            exercise="Run", duration_sec=1200,
            target_heart_rate_zone=2, target_power_zone=3,
        )], kind="running")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["zoneNumber"] == 2


def test_pace_target_uses_value_pair_not_zone_number():
    """Live-verified: pace.zone (workoutTargetTypeId 6) did NOT accept
    zoneNumber in testing, unlike heart rate/power — only an explicit
    targetValueOne/targetValueTwo pair."""
    payload = build_strength_payload(
        session([ExerciseStep(
            exercise="400m repeat", distance_m=400,
            target_pace_min_mps=4.0, target_pace_max_mps=4.5,
        )], kind="running")
    )
    (step,) = payload["workoutSegments"][0]["workoutSteps"]
    assert step["targetType"] == {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
    assert step["targetValueOne"] == 4.0
    assert step["targetValueTwo"] == 4.5
    assert "zoneNumber" not in step


def test_superset_only_merges_consecutive_same_group_steps():
    """A repeated group id that ISN'T consecutive (group 1, group 2, group 1)
    is two separate supersets, not one merged group — reordering exercises
    the athlete/Claude explicitly sequenced would be a worse bug than not
    merging at all."""
    payload = build_strength_payload(
        session([
            ExerciseStep(exercise="Wall sit", sets=2, duration_sec=30, superset_group=1),
            ExerciseStep(exercise="Row", sets=2, reps=10, superset_group=1),
            ExerciseStep(exercise="Plank", sets=1, duration_sec=40),
            ExerciseStep(exercise="Wall sit", sets=2, duration_sec=30, superset_group=1),
        ])
    )
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 3  # [superset(2 ex), plank, superset(1 ex, sets>1 so still wrapped)]
    assert len(steps[0]["workoutSteps"]) == 2
    assert steps[1]["type"] == "ExecutableStepDTO"  # plank, ungrouped, sets=1 stays flat
    assert steps[2]["type"] == "RepeatGroupDTO"  # the second "wall sit" alone, own block


def test_ungrouped_steps_are_unaffected_by_superset_logic():
    payload = build_strength_payload(
        session([
            ExerciseStep(exercise="Goblet squat", sets=3, reps=8),
            ExerciseStep(exercise="Side plank", sets=1, duration_sec=40),
        ])
    )
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["type"] == "RepeatGroupDTO"
    assert steps[1]["type"] == "ExecutableStepDTO"


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
