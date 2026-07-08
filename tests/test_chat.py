from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jim.agent.loop import RunReport
from jim.chat import format_proposal, handle_chat_message, resolve_target_day
from jim.schemas import ExerciseStep, StructuredSession

TODAY = date(2026, 7, 8)
MORNING = datetime(2026, 7, 8, 7, 30)
EVENING = datetime(2026, 7, 8, 20, 0)


def test_morning_message_targets_today():
    target, note = resolve_target_day("knee sore, keep it light", MORNING)
    assert target == TODAY
    assert note == "knee sore, keep it light"


def test_evening_message_targets_tomorrow():
    target, _ = resolve_target_day("want upper body", EVENING)
    assert target == TODAY + timedelta(days=1)


def test_explicit_keyword_overrides_time_of_day():
    target, note = resolve_target_day("tomorrow: home only, 30 min", MORNING)
    assert target == TODAY + timedelta(days=1)
    assert note == "home only, 30 min"
    target, note = resolve_target_day("Today easy spin", EVENING)
    assert target == TODAY
    assert note == "easy spin"


@dataclass
class FakeRunner:
    seen: dict = None

    def __call__(self, today, plan_for=None, checkin=None, **kw):
        self.seen = {"today": today, "plan_for": plan_for, "checkin": checkin}
        return RunReport(
            for_date=plan_for,
            session=StructuredSession(
                for_date=plan_for,
                kind="mobility",
                title="PT Day · Home",
                garmin_workout_id="1624611562",
                template_key="pt_home",
                est_duration_min=30,
                rationale_summary="pain elevated; home PT per check-in",
            ),
        )


def test_handle_chat_message_runs_replan_with_checkin():
    runner = FakeRunner()
    reply = handle_chat_message("knee flaring, home, 30 min", MORNING, runner=runner)
    assert runner.seen["plan_for"] == TODAY
    ci = runner.seen["checkin"]
    assert ci.note == "knee flaring, home, 30 min"
    assert ci.for_date == TODAY
    assert ci.edited_ts == MORNING
    assert "PT Day · Home" in reply
    assert "today" in reply
    assert "pt_home" in reply  # scheduled-by-id note


def test_handle_chat_message_survives_runner_failure():
    def boom(*a, **kw):
        raise RuntimeError("garmin down")

    reply = handle_chat_message("hello", MORNING, runner=boom)
    assert "Couldn't build a plan" in reply


def test_format_proposal_lists_steps_for_adapted_sessions():
    report = RunReport(
        for_date=TODAY + timedelta(days=1),
        session=StructuredSession(
            for_date=TODAY + timedelta(days=1),
            kind="strength",
            title="Modified upper",
            steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8, weight_kg=40)],
            est_duration_min=40,
        ),
    )
    text = format_proposal(report, today=TODAY)
    assert "tomorrow" in text
    assert "Bench press — 3x8 @ 40.0kg" in text
