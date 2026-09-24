"""Plan-vs-actual tracking and get_week_overview.

Write tools record/update/cancel `suggestions` rows (via tools.memory) so
the overview can compare what was planned with what Garmin recorded. The
DB and Garmin are both faked; memory functions are swapped for in-memory
stand-ins so the wiring, not SQL, is what's under test (the SQL is covered
by test_multi_user_isolation's fake DB)."""

from datetime import date

import pytest

import jim.mcp_server as m
import jim.tools.garmin as garmin_mod
from jim import db
from jim.jobs.reconcile import adhered
from jim.schemas import ActivitySummary, StructuredSession, WorkoutRef
from jim.tools import history, memory

TODAY = date(2026, 9, 24)


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(m, "_current_user_id", lambda: 7)
    monkeypatch.setattr(m, "_user_today", lambda uid: TODAY)
    monkeypatch.setattr(m, "_ensure_history", lambda uid: None)
    yield
    garmin_mod._clients.pop(7, None)


@pytest.fixture
def log(monkeypatch):
    calls = []
    for name in ("record_plan", "update_plan", "cancel_plans"):
        monkeypatch.setattr(
            memory, name, lambda *a, _n=name, **k: calls.append((_n, a, k)),
        )
    return calls


# --- write tools keep the plan record in step ----------------------------------------------


def test_create_records_the_plan(monkeypatch, log):
    monkeypatch.setattr(garmin_mod, "create_garmin_workout",
                        lambda u, s: WorkoutRef(workout_id="11"))
    monkeypatch.setattr(garmin_mod, "schedule_workout", lambda *a: None)
    _fn(m.create_or_update_workout)(
        for_date="2026-09-25", title="Legs", kind="strength",
        steps=[m.StepIn(exercise="Goblet squat", reps=8)],
    )
    ((name, args, _),) = log
    assert name == "record_plan" and args[:2] == (7, "11")
    assert args[2].for_date == date(2026, 9, 25) and args[2].steps[0].reps == 8


def test_schedule_records_the_library_workout_with_its_planned_reps(monkeypatch, log):
    detail = {"workoutName": "Full Body A2", "sportType": {"sportTypeKey": "strength_training"},
              "workoutSegments": [{"workoutSteps": [{
                  "type": "RepeatGroupDTO", "numberOfIterations": 3, "workoutSteps": [{
                      "type": "ExecutableStepDTO", "stepType": {"stepTypeKey": "interval"},
                      "exerciseName": "SEATED_CABLE_ROW",
                      "endCondition": {"conditionTypeKey": "reps"},
                      "endConditionValue": 12.0, "weightValue": 18.12}]}]}]}
    monkeypatch.setattr(garmin_mod, "schedule_workout", lambda *a: None)
    monkeypatch.setattr(garmin_mod, "get_garmin_workout_detail", lambda u, w: detail)
    _fn(m.schedule_workout)(workout_id="55", on="2026-09-26")
    ((name, args, _),) = log
    plan = args[2]
    assert name == "record_plan" and args[1] == "55"
    assert plan.kind == "strength" and plan.title == "Full Body A2"
    step = plan.steps[0]
    assert (step.exercise, step.sets, step.reps) == ("SEATED_CABLE_ROW", 3, 12)


def test_schedule_still_succeeds_if_recording_fails(monkeypatch):
    monkeypatch.setattr(garmin_mod, "schedule_workout", lambda *a: None)

    def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(garmin_mod, "get_garmin_workout_detail", boom)
    assert _fn(m.schedule_workout)(workout_id="55", on="2026-09-26")["ok"] is True


def test_update_refreshes_future_plans(monkeypatch, log):
    monkeypatch.setattr(garmin_mod, "update_garmin_workout",
                        lambda u, w, s: WorkoutRef(workout_id=w))
    _fn(m.update_workout)(workout_id="55", title="Full Body A2", kind="strength",
                          steps=[m.StepIn(exercise="Row", reps=10)])
    ((name, args, _),) = log
    assert name == "update_plan" and args[1] == "55" and args[3] == TODAY


def test_unschedule_cancels_only_what_was_removed_on_that_day(monkeypatch, log):
    monkeypatch.setattr(garmin_mod, "clear_schedule",
                        lambda u, d, w: [{"workout_id": "55", "title": "A"}])
    _fn(m.unschedule_day)(on="2026-09-26")
    ((name, args, kwargs),) = log
    assert name == "cancel_plans" and args == (7, ["55"]) and kwargs == {"on": date(2026, 9, 26)}


def test_delete_cancels_today_and_future_plans_only(monkeypatch, log):
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", lambda u, w: None)
    _fn(m.delete_workout)(workout_id="55")
    ((name, args, kwargs),) = log
    assert name == "cancel_plans" and kwargs == {"from_day": TODAY}


# --- reconcile / adherence ---------------------------------------------------------------------


def _plan(kind):
    return StructuredSession(for_date=TODAY, kind=kind, title="x")


def _act(type_):
    return ActivitySummary(activity_id="1", type=type_, duration_min=30)


@pytest.mark.parametrize("kind,type_", [
    ("walking", "walking"), ("running", "treadmill_running"), ("yoga", "yoga"),
    ("cycling", "indoor_cycling"), ("strength", "strength_training"), ("other", "anything"),
])
def test_every_kind_can_be_matched(kind, type_):
    """A planned walk used to read as never done: 'walking' wasn't a key."""
    assert adhered(_plan(kind), [_act(type_)])[0] is True


def test_a_different_session_is_named_not_just_missed():
    ok, note = adhered(_plan("strength"), [_act("walking")])
    assert not ok and "did walking instead" in note


# --- get_week_overview -------------------------------------------------------------------------


class _Api:
    def get_scheduled_workouts(self, year, month):
        return {"calendarItems": [
            {"itemType": "workout", "date": "2026-09-22", "id": 1, "workoutId": 300,
             "title": "PT Day - Gym"},
            {"itemType": "workout", "date": "2026-09-26", "id": 2, "workoutId": 200,
             "title": "Full Body A2"},
        ]}

    def get_workouts(self, start=0, limit=200):
        return [{"workoutId": 200, "workoutName": "Full Body A2",
                 "sportType": {"sportTypeKey": "strength_training"}},
                {"workoutId": 300, "workoutName": "PT Day - Gym",
                 "sportType": {"sportTypeKey": "mobility"}}]


def test_week_overview_end_to_end(monkeypatch):
    garmin_mod._clients[7] = _Api()
    monkeypatch.setattr(history, "readiness_read", lambda u, d: type("R", (), {
        "model_dump": lambda self, mode=None: {
            "status": "steady", "headline": "Steady", "detail": "x", "acwr": 1.0,
            "body_battery": 80, "hrv": 60.0, "sleep_hours": 7.5}})())
    monkeypatch.setattr(history, "activities_between", lambda u, s, e: [
        {"day": date(2026, 9, 21), "activity_id": "a1", "type": "strength_training",
         "duration_min": 60},
        {"day": date(2026, 9, 22), "activity_id": "a2", "type": "walking", "duration_min": 30},
    ])
    monkeypatch.setattr(history, "exercise_sets_since", lambda u, s: [
        {"day": date(2026, 9, 21), "exercise_name": "SEATED_CABLE_ROW", "category": None,
         "reps": r, "weight_kg": 18.12, "duration_sec": None} for r in (15, 15, 14)
    ])
    monkeypatch.setattr(memory, "planned_between", lambda u, s, e: [
        {"id": 1, "for_date": date(2026, 9, 21), "workout_id": "200", "source": "mcp",
         "plan": {"title": "Full Body A2", "kind": "strength",
                  "steps": [{"exercise": "SEATED_CABLE_ROW", "reps": 15}]}},
        {"id": 2, "for_date": date(2026, 9, 23), "workout_id": "400", "source": "mcp",
         "plan": {"title": "Jim · Walk", "kind": "walking", "steps": []}},
    ])
    monkeypatch.setattr(db, "get_constraints", lambda u: "no deep knee flexion")

    out = _fn(m.get_week_overview)()

    statuses = {r["title"]: r["status"] for r in out["last_7_days"]["planned_vs_done"]}
    assert statuses == {
        "Full Body A2": "done",          # Jim's record + a strength session that day
        "PT Day - Gym": "did_something_else",  # still on calendar; walked instead
        "Jim · Walk": "missed",
    }
    pt = next(r for r in out["last_7_days"]["planned_vs_done"] if r["title"] == "PT Day - Gym")
    assert pt["still_on_calendar"] is True
    assert out["last_7_days"]["summary"] == "1 of 3 planned sessions done as planned"
    assert [u["title"] for u in out["next_7_days"]] == ["Full Body A2"]
    (row,) = out["progression"]
    assert row["exercise"] == "SEATED_CABLE_ROW" and row["unit"] == "lb"
    assert row["target_reps"] == "11-15"   # from the plan's 15 reps
    assert row["action"] == "increase" and row["next_load"] == "45 lb"
    assert out["constraints"] == "no deep knee flexion"


def test_week_overview_holds_increases_in_a_heavy_week(monkeypatch):
    garmin_mod._clients[7] = _Api()
    monkeypatch.setattr(history, "readiness_read", lambda u, d: type("R", (), {
        "model_dump": lambda self, mode=None: {
            "status": "ease", "headline": "Ease off today", "detail": "x", "acwr": 1.7,
            "body_battery": None, "hrv": None, "sleep_hours": None}})())
    monkeypatch.setattr(history, "activities_between", lambda u, s, e: [])
    monkeypatch.setattr(history, "exercise_sets_since", lambda u, s: [
        {"day": date(2026, 9, 21), "exercise_name": "ROPE_PRESSDOWN", "category": None,
         "reps": 12, "weight_kg": 9.06, "duration_sec": None}])
    monkeypatch.setattr(memory, "planned_between", lambda u, s, e: [])
    monkeypatch.setattr(db, "get_constraints", lambda u: "")

    out = _fn(m.get_week_overview)()
    (row,) = out["progression"]
    assert row["action"] == "hold" and "Ease off today" in row["reason"]
    assert "recovery_note" in out["readiness"]
    assert any("ACWR 1.7" in c for c in out["suggested_changes"])
    assert out["constraints"].startswith("(none recorded")


def test_week_overview_survives_an_unreadable_calendar(monkeypatch):
    class Broken(_Api):
        def get_scheduled_workouts(self, year, month):
            return None

    garmin_mod._clients[7] = Broken()
    monkeypatch.setattr(history, "readiness_read", lambda u, d: type("R", (), {
        "model_dump": lambda self, mode=None: {"status": "steady", "acwr": None}})())
    monkeypatch.setattr(history, "activities_between", lambda u, s, e: [])
    monkeypatch.setattr(history, "exercise_sets_since", lambda u, s: [])
    monkeypatch.setattr(memory, "planned_between", lambda u, s, e: [
        {"id": 9, "for_date": date(2026, 9, 27), "workout_id": "200", "source": "mcp",
         "plan": {"title": "Full Body A2", "kind": "strength", "steps": []}}])
    monkeypatch.setattr(db, "get_constraints", lambda u: "x")

    out = _fn(m.get_week_overview)()
    assert "unavailable" in out["next_7_days"]
    assert out["next_7_days"]["from_jims_records"][0]["title"] == "Full Body A2"
