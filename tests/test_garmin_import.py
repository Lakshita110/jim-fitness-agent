"""Importing an existing Garmin workout into the playbook — the reverse of
build_strength_payload/build_template_payload. See tools/garmin.py's
parse_workout_to_template and app.py's /api/garmin/workouts* routes."""

from jim.tools.garmin import list_garmin_workouts, parse_workout_to_template


def test_parses_a_jim_authored_workout_back_into_a_template():
    """A workout Jim itself built (build_strength_payload's shape, with a
    `description` on every step) should round-trip its names/doses exactly."""
    raw = {
        "workoutId": 12345,
        "workoutName": "Full Body A",
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
        "workoutSegments": [
            {
                "workoutSteps": [
                    {
                        "type": "RepeatGroupDTO",
                        "numberOfIterations": 3,
                        "workoutSteps": [
                            {
                                "type": "ExecutableStepDTO",
                                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                                "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
                                "endConditionValue": 8,
                                "description": "Goblet squat",
                                "category": "SQUAT",
                                "exerciseName": "GOBLET_SQUAT",
                            }
                        ],
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                        "endConditionValue": 40,
                        "description": "Side plank",
                    },
                ]
            }
        ],
    }
    template = parse_workout_to_template("full_body_a", raw)
    assert template.label == "Full Body A"
    assert template.garmin_workout_id == "12345"
    assert template.sport == "strength"
    assert template.warmup == []
    (block,) = template.blocks
    squat, plank = block.exercises
    assert squat.name == "Goblet squat"
    assert squat.sets == 3
    assert squat.reps == 8
    assert plank.name == "Side plank"
    assert plank.sets is None
    assert plank.time_sec == 40


def test_falls_back_to_exercise_name_when_there_is_no_description():
    """A workout built on the watch or in Garmin Connect (not by Jim) has no
    `description` field — only category/exerciseName."""
    raw = {
        "workoutId": 999,
        "workoutName": "Watch-built workout",
        "sportType": {"sportTypeKey": "strength_training"},
        "workoutSegments": [
            {
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": {"stepTypeKey": "interval"},
                        "endCondition": {"conditionTypeKey": "reps"},
                        "endConditionValue": 10,
                        "category": "SQUAT",
                        "exerciseName": "GOBLET_SQUAT",
                    }
                ]
            }
        ],
    }
    template = parse_workout_to_template("watch_workout", raw)
    (block,) = template.blocks
    (ex,) = block.exercises
    assert ex.name == "Goblet Squat"


def test_warmup_steps_are_split_out_from_the_main_block():
    raw = {
        "workoutId": 1,
        "workoutName": "With warmup",
        "sportType": {"sportTypeKey": "strength_training"},
        "workoutSegments": [
            {
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": {"stepTypeKey": "warmup"},
                        "endCondition": {"conditionTypeKey": "time"},
                        "endConditionValue": 300,
                        "description": "Bike",
                    },
                    {
                        "type": "ExecutableStepDTO",
                        "stepType": {"stepTypeKey": "interval"},
                        "endCondition": {"conditionTypeKey": "reps"},
                        "endConditionValue": 12,
                        "description": "Lat pulldown",
                    },
                ]
            }
        ],
    }
    template = parse_workout_to_template("with_warmup", raw)
    (warm,) = template.warmup
    assert warm.name == "Bike"
    assert warm.time_sec == 300
    (block,) = template.blocks
    assert block.exercises[0].name == "Lat pulldown"


def test_list_garmin_workouts_returns_id_name_sport(monkeypatch):
    class FakeApi:
        def get_workouts(self, start=0, limit=100):
            return [
                {
                    "workoutId": 42,
                    "workoutName": "Full Body B",
                    "sportType": {"sportTypeKey": "strength_training"},
                }
            ]

    monkeypatch.setattr("jim.tools.garmin.client", lambda user_id: FakeApi())
    workouts = list_garmin_workouts(user_id=1)
    assert workouts == [
        {"workout_id": "42", "name": "Full Body B", "sport": "strength_training"}
    ]
