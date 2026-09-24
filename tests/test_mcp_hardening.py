"""Input validation and failure handling in mcp_server's tools: bad input
gets a ToolError saying what to fix (not a raw ValueError or Garmin HTTP
error), Garmin failures get translated, and half-finished writes clean up
after themselves. Tool bodies are called directly (`.fn`) with the auth
lookup and Garmin layer faked out, so none of this touches the network."""

from datetime import date

import pytest
from fastmcp.exceptions import ToolError
from garminconnect import GarminConnectAuthenticationError

import jim.mcp_server as m
import jim.tools.garmin as garmin_mod
from jim import db
from jim.schemas import WorkoutRef


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _as_user_7(monkeypatch):
    monkeypatch.setattr(m, "_current_user_id", lambda: 7)
    monkeypatch.setattr(m, "_user_today", lambda uid: date(2026, 9, 24))
    yield
    garmin_mod._clients.pop(7, None)


def _step(**kw):
    return m.StepIn(**{"exercise": "Goblet squat", "reps": 8, **kw})


# --- parsing helpers ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "tomorrow", "2026-13-01", "24/09/2026"])
def test_bad_dates_are_a_clear_tool_error(bad):
    with pytest.raises(ToolError, match="ISO date"):
        m._parse_date(bad, "for_date")


def test_dates_tolerate_surrounding_whitespace():
    assert m._parse_date(" 2026-09-24 ", "on") == date(2026, 9, 24)


@pytest.mark.parametrize("bad", ["", "abc", "12a", "-5", "0", "Full Body A"])
def test_bad_workout_ids_are_a_clear_tool_error(bad):
    with pytest.raises(ToolError, match="numeric id"):
        m._parse_workout_id(bad)


def test_workout_id_accepts_ints_and_padded_strings():
    assert m._parse_workout_id(123) == "123"
    assert m._parse_workout_id(" 456 ") == "456"


def test_day_windows_are_bounded():
    with pytest.raises(ToolError, match="between 1 and 90"):
        _fn(m.get_recent_activities)(days=0)
    with pytest.raises(ToolError, match="between 1 and 730"):
        _fn(m.get_exercise_history)(exercise="squat", days=5000)
    with pytest.raises(ToolError, match="between 0 and 120"):
        _fn(m.backfill_history)(days=365)


# --- step validation ------------------------------------------------------------


def test_empty_steps_rejected_for_a_real_session():
    with pytest.raises(ToolError, match="at least one step"):
        m._to_steps([], "strength")


def test_empty_steps_allowed_for_a_rest_day():
    assert m._to_steps([], "rest") == []


def test_pyramid_group_without_rounds_is_caught_before_garmin():
    """Otherwise the outer repeat goes out with numberOfIterations=None,
    which Garmin rejects with an opaque error."""
    with pytest.raises(ToolError, match="pyramid_rounds"):
        m._to_steps([_step(pyramid_group=1)], "strength")


@pytest.mark.parametrize("field,value", [
    ("reps", 0), ("duration_sec", -30), ("distance_m", 0),
    ("end_at_heart_rate_bpm", -1), ("weight_kg", -5), ("sets", 0),
])
def test_nonsense_step_numbers_are_rejected(field, value):
    with pytest.raises(ToolError, match="step 1"):
        m._to_steps([_step(**{field: value})], "strength")


def test_out_of_range_zone_is_rejected():
    with pytest.raises(ToolError, match="1-10"):
        m._to_steps([_step(target_heart_rate_zone=12)], "running")


def test_blank_exercise_name_is_rejected():
    with pytest.raises(ToolError, match="exercise name is empty"):
        m._to_steps([_step(exercise="   ")], "strength")


# --- Garmin error translation ------------------------------------------------------


def test_auth_failure_becomes_actionable_and_evicts_the_cached_client():
    garmin_mod._clients[7] = object()
    with pytest.raises(ToolError, match="reconnect Garmin"):
        with m._garmin(7, "do a thing"):
            raise GarminConnectAuthenticationError("401")
    assert 7 not in garmin_mod._clients


def test_not_connected_runtime_error_passes_its_message_through():
    with pytest.raises(ToolError, match="has not connected Garmin"):
        with m._garmin(7, "list workouts"):
            raise RuntimeError("user 7 has not connected Garmin")


def test_unexpected_errors_name_the_action():
    with pytest.raises(ToolError, match="couldn't read workout 5: KeyError"):
        with m._garmin(7, "read workout 5"):
            raise KeyError("workoutSegments")


def test_best_effort_returns_a_marker_instead_of_raising():
    def boom(uid, day):
        raise ConnectionError("down")

    result = m._best_effort(7, "x", boom, date(2026, 9, 24))
    assert "unavailable" in result and "down" in result["unavailable"]


# --- create_or_update_workout --------------------------------------------------------


def test_schedule_failure_deletes_the_just_created_workout(monkeypatch):
    monkeypatch.setattr(garmin_mod, "create_garmin_workout",
                        lambda uid, s: WorkoutRef(workout_id="900"))

    def fail_schedule(uid, wid, on):
        raise RuntimeError("calendar service down")

    deleted = []
    monkeypatch.setattr(garmin_mod, "schedule_workout", fail_schedule)
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout",
                        lambda uid, wid: deleted.append(wid))

    with pytest.raises(ToolError, match="deleted again, so nothing changed"):
        _fn(m.create_or_update_workout)(
            for_date="2026-09-25", title="Legs", kind="strength", steps=[_step()],
        )
    assert deleted == ["900"]


def test_schedule_and_cleanup_both_failing_reports_the_orphan_id(monkeypatch):
    monkeypatch.setattr(garmin_mod, "create_garmin_workout",
                        lambda uid, s: WorkoutRef(workout_id="901"))

    def boom(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(garmin_mod, "schedule_workout", boom)
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", boom)

    with pytest.raises(ToolError, match="901 was created but is NOT on the calendar"):
        _fn(m.create_or_update_workout)(
            for_date="2026-09-25", title="Legs", kind="strength", steps=[_step()],
        )


def test_prefix_is_not_doubled_when_the_title_already_has_it(monkeypatch):
    seen = []
    monkeypatch.setattr(garmin_mod, "create_garmin_workout",
                        lambda uid, s: seen.append(s.title) or WorkoutRef(workout_id="1"))
    monkeypatch.setattr(garmin_mod, "schedule_workout", lambda *a: None)

    result = _fn(m.create_or_update_workout)(
        for_date="2026-09-25", title="Jim · Legs", kind="strength", steps=[_step()],
    )
    assert seen == ["Jim · Legs"]
    assert result["title"] == "Jim · Legs"


# --- save_to_library / update_workout ---------------------------------------------------


def test_permanent_workout_cannot_carry_the_cleanup_prefix():
    with pytest.raises(ToolError, match="drop it"):
        _fn(m.save_to_library)(title="Jim · Full Body A", kind="strength", steps=[_step()])


def test_update_rejects_a_non_numeric_id_before_calling_garmin(monkeypatch):
    monkeypatch.setattr(garmin_mod, "update_garmin_workout",
                        lambda *a: pytest.fail("should not reach Garmin"))
    with pytest.raises(ToolError, match="numeric id"):
        _fn(m.update_workout)(workout_id="Full Body A", title="x", kind="strength",
                              steps=[_step()])


# --- calendar tools --------------------------------------------------------------------


def test_scheduled_range_must_be_ordered_and_bounded():
    with pytest.raises(ToolError, match="before start"):
        _fn(m.get_scheduled_workouts)(start="2026-09-30", end="2026-09-01")
    with pytest.raises(ToolError, match="92 days"):
        _fn(m.get_scheduled_workouts)(start="2026-01-01", end="2026-12-31")


class _FakeCalendarApi:
    def __init__(self):
        self.unscheduled = []

    def get_scheduled_workouts(self, year, month):
        return {"calendarItems": [
            {"itemType": "workout", "date": "2026-09-25", "id": 11,
             "workoutId": 100, "title": "Full Body A"},
            {"itemType": "workout", "date": "2026-09-25", "id": 12,
             "workoutId": 200, "title": "Jim · Walk"},
            {"itemType": "activity", "date": "2026-09-25", "id": 13},
            {"itemType": "workout", "date": "2026-09-26", "id": 14,
             "workoutId": 100, "title": "Full Body A"},
        ]}

    def unschedule_workout(self, scheduled_id):
        self.unscheduled.append(scheduled_id)


def test_unschedule_day_can_target_one_workout_and_reports_what_it_removed():
    api = _FakeCalendarApi()
    garmin_mod._clients[7] = api
    result = _fn(m.unschedule_day)(on="2026-09-25", workout_id="200")
    assert api.unscheduled == [12]
    assert result["removed"] == [{"workout_id": "200", "title": "Jim · Walk"}]


def test_unschedule_day_without_id_clears_only_that_days_workouts():
    api = _FakeCalendarApi()
    garmin_mod._clients[7] = api
    result = _fn(m.unschedule_day)(on="2026-09-25")
    assert api.unscheduled == [11, 12]  # not the activity, not the 26th
    assert len(result["removed"]) == 2


# --- get_saved_workout ---------------------------------------------------------------------


def test_saved_workout_detail_strips_nulls_but_keeps_zeros_and_falses():
    raw = {"workoutName": "A", "description": None, "estimatedDurationInSecs": 0,
           "shared": False, "workoutSegments": [{"workoutSteps": [
               {"description": "Squat", "category": None, "weightValue": None,
                "targetType": {}, "zoneNumber": None, "endConditionValue": 8.0}]}]}
    assert m._prune(raw) == {
        "workoutName": "A", "estimatedDurationInSecs": 0, "shared": False,
        "workoutSegments": [{"workoutSteps": [
            {"description": "Squat", "endConditionValue": 8.0}]}],
    }


# --- constraints --------------------------------------------------------------------------


def test_set_constraints_refuses_an_accidental_wipe(monkeypatch):
    monkeypatch.setattr(db, "set_constraints",
                        lambda uid, c: pytest.fail("should not write"))
    with pytest.raises(ToolError, match="allow_empty=True"):
        _fn(m.set_constraints)(content="   ")


def test_set_constraints_wipes_only_when_explicitly_allowed(monkeypatch):
    writes = []
    monkeypatch.setattr(db, "set_constraints", lambda uid, c: writes.append((uid, c)))
    _fn(m.set_constraints)(content="", allow_empty=True)
    assert writes == [(7, "")]
