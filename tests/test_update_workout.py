"""update_garmin_workout: edits an existing Garmin workout IN PLACE via a PUT
to the same per-id path get_workout_by_id/delete_workout already use,
instead of the create-new/repoint/delete-old dance every other write tool
here has to document as the only way to "edit" a workout. Undocumented
(found only by noticing the underlying HTTP client exposes a `put`
alongside its `post`/`delete`), so this pins down exactly what gets called
and with what payload shape."""

from datetime import date

import jim.tools.garmin as garmin_mod
from jim.schemas import ExerciseStep, StructuredSession


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def put(self, domain, path, **kwargs):
        self.calls.append((domain, path, kwargs))
        return self.response


class FakeGarminApi:
    def __init__(self, http_client):
        self.garmin_workouts = "/workout-service"
        self.client = http_client


def _session() -> StructuredSession:
    return StructuredSession(
        for_date=date(2026, 7, 8),
        kind="strength",
        title="Full Body A",
        steps=[ExerciseStep(exercise="Goblet squat", sets=3, reps=8)],
    )


def test_update_hits_the_per_id_workout_path_with_a_put(monkeypatch):
    http_client = FakeHttpClient(response={"workoutId": 999})
    garmin_mod._clients[1] = FakeGarminApi(http_client)
    monkeypatch.setattr(
        "jim.tools.exercise_match.semantic_resolver", lambda user_id: None
    )

    ref = garmin_mod.update_garmin_workout(1, "999", _session())

    assert ref.workout_id == "999"
    (call,) = http_client.calls
    domain, path, kwargs = call
    assert path == "/workout-service/workout/999"
    assert kwargs["api"] is True
    assert kwargs["json"]["workoutId"] == 999
    assert kwargs["json"]["workoutName"] == "Full Body A"


def test_update_falls_back_to_the_given_id_if_response_omits_workoutId(monkeypatch):
    http_client = FakeHttpClient(response=None)
    garmin_mod._clients[2] = FakeGarminApi(http_client)
    monkeypatch.setattr(
        "jim.tools.exercise_match.semantic_resolver", lambda user_id: None
    )

    ref = garmin_mod.update_garmin_workout(2, "555", _session())

    assert ref.workout_id == "555"


def test_create_or_update_workout_also_schedules_on_for_date(monkeypatch):
    """Reported bug: for_date was set but the workout never landed on the
    calendar — create_or_update_workout only created it. It now schedules
    too, so the athlete doesn't need a second schedule_workout call."""
    import jim.mcp_server as mcp_server_mod
    from jim.schemas import WorkoutRef

    monkeypatch.setattr(mcp_server_mod, "_current_user_id", lambda: 7)
    monkeypatch.setattr(
        garmin_mod, "create_garmin_workout", lambda uid, s: WorkoutRef(workout_id="123")
    )
    scheduled = []
    monkeypatch.setattr(
        garmin_mod, "schedule_workout", lambda uid, wid, on: scheduled.append((uid, wid, on))
    )

    tool = mcp_server_mod.create_or_update_workout
    fn = getattr(tool, "fn", tool)
    result = fn(
        for_date="2026-09-25", title="Thursday", kind="strength",
        steps=[mcp_server_mod.StepIn(exercise="Goblet squat", reps=8)],
    )

    assert scheduled == [(7, "123", date(2026, 9, 25))]
    assert result["workout_id"] == "123"
    assert result["scheduled_for"] == "2026-09-25"
