"""Importing an existing Garmin workout into the playbook — the reverse of
build_strength_payload/build_template_payload. See tools/garmin.py's
parse_workout_to_template and app.py's /api/garmin/workouts* routes."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jim.app as app_mod
from jim.auth import User
from jim.playbook import Playbook, WorkoutTemplate
from jim.tools.garmin import list_garmin_workouts, parse_workout_to_template

client = TestClient(app_mod.app)
TEST_USER = User(id=7, email="athlete2@example.com")


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    monkeypatch.setattr(app_mod, "_ready", lambda: None)
    monkeypatch.setattr(
        app_mod, "settings",
        lambda: SimpleNamespace(app_timezone="America/New_York", cron_secret="cr0n"),
    )
    client.cookies.clear()
    yield
    client.cookies.clear()


def _sign_in(monkeypatch, user=TEST_USER):
    monkeypatch.setattr(app_mod.auth, "authenticate", lambda email, password: user)
    monkeypatch.setattr(
        app_mod.auth, "get_user_by_id", lambda uid: user if uid == user.id else None
    )
    r = client.post("/auth/login", json={"email": user.email, "password": "irrelevant"})
    assert r.status_code == 200, r.text


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


# --- route: GET /api/garmin/workouts enrichment ------------------------------


def test_route_flags_jim_created_and_already_in_playbook(monkeypatch):
    monkeypatch.setattr(
        "jim.tools.garmin.list_garmin_workouts",
        lambda user_id: [
            {"workout_id": "1", "name": "Full Body A", "sport": "strength_training"},
            {"workout_id": "2", "name": "Full Body A — adapted 2026-07-09",
             "sport": "strength_training"},
            {"workout_id": "3", "name": "Home PT", "sport": "mobility"},
        ],
    )
    kv = {(7, "jim_created_workouts"): {
        "2026-07-09": {"workout_id": "2", "template_key": "full_body_a"},
    }}
    monkeypatch.setattr("jim.db.kv_get", lambda uid, key: kv.get((uid, key)))
    monkeypatch.setattr(
        app_mod, "load_playbook",
        lambda uid: Playbook(
            rotation=["a"],
            workouts={"a": WorkoutTemplate(key="a", label="Full Body A",
                                           garmin_workout_id="1", sport="strength_training")},
        ),
    )
    _sign_in(monkeypatch)

    r = client.get("/api/garmin/workouts")
    assert r.status_code == 200
    by_id = {w["workout_id"]: w for w in r.json()["workouts"]}
    assert by_id["1"]["already_in_playbook"] is True
    assert by_id["1"]["jim_created"] is False
    assert by_id["2"]["already_in_playbook"] is False
    assert by_id["2"]["jim_created"] is True
    assert by_id["2"]["template_key"] == "full_body_a"
    assert by_id["2"]["for_date"] == "2026-07-09"
    assert by_id["3"]["jim_created"] is False
    assert by_id["3"]["already_in_playbook"] is False


def test_successful_import_pops_the_kv_tracking_entry(monkeypatch):
    monkeypatch.setattr(
        "jim.tools.garmin.get_garmin_workout_detail",
        lambda user_id, workout_id: {
            "workoutId": 2, "workoutName": "Full Body A — adapted 2026-07-09",
            "sportType": {"sportTypeKey": "strength_training"},
            "workoutSegments": [{"workoutSteps": [
                {"type": "ExecutableStepDTO", "stepType": {"stepTypeKey": "interval"},
                 "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 8,
                 "description": "Goblet squat"},
            ]}],
        },
    )
    kv = {(7, "jim_created_workouts"): {
        "2026-07-09": {"workout_id": "2", "template_key": "full_body_a"},
        "2026-07-10": {"workout_id": "99", "template_key": None},
    }}
    monkeypatch.setattr("jim.db.kv_get", lambda uid, key: kv.get((uid, key)))
    monkeypatch.setattr("jim.db.kv_set", lambda uid, key, value: kv.__setitem__((uid, key), value))
    store: dict[int, Playbook] = {}

    def load(uid):
        return store.get(uid, Playbook())

    def save(uid, pb):
        store[uid] = pb

    # promote_garmin_workout (jim.playbook) and this route both resolve
    # load_playbook/save_playbook as globals in their own module, so both
    # bindings need patching to share the same fake store.
    monkeypatch.setattr(app_mod, "load_playbook", load)
    monkeypatch.setattr(app_mod, "save_playbook", save)
    monkeypatch.setattr("jim.playbook.load_playbook", load)
    monkeypatch.setattr("jim.playbook.save_playbook", save)
    _sign_in(monkeypatch)

    r = client.post("/api/garmin/workouts/import",
                    json={"workout_id": "2", "key": "full_body_a"})
    assert r.status_code == 200, r.text

    remaining = kv[(7, "jim_created_workouts")]
    assert set(remaining) == {"2026-07-10"}  # only the promoted one is popped
