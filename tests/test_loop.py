"""Agent loop tests with an injected fake toolbox — no live APIs (PLAN.md §13)."""

from datetime import date, timedelta

import pytest

from vesper.agent.loop import Toolbox, ToolBudgetExceeded, run_agent
from vesper.schemas import (
    CheckIn,
    ExerciseStep,
    GarminToday,
    HistoryFeatures,
    NotionDay,
    ResearchHit,
    StructuredSession,
)

TODAY = date(2026, 7, 6)
TOMORROW = TODAY + timedelta(days=1)


def sane_session(for_date, **overrides) -> StructuredSession:
    base = StructuredSession(
        for_date=for_date,
        kind="strength",
        title="Upper push",
        steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8)],
        est_duration_min=45,
        rationale_summary="push day",
    )
    return base.model_copy(update=overrides)


class Recorder:
    """Fake toolbox that records every call for assertions."""

    def __init__(
        self, garmin=None, notion=None, features=None, compose_outputs=None, checkin=None
    ):
        self.calls: list[str] = []
        self.written = None
        self.recorded = None
        self._garmin = garmin or GarminToday(day=TODAY, readiness=70, body_battery=60)
        self._notion = notion or NotionDay(day=TODAY, pain_level=1, pt_done=True)
        self._checkin = checkin or CheckIn(for_date=TOMORROW)
        self._features = features or HistoryFeatures(
            as_of=TODAY, window_days=28, weekly_volume_min=200, days_since_legs=3
        )
        self._compose_outputs = compose_outputs or [sane_session(TOMORROW)]
        self._compose_i = 0

    def toolbox(self) -> Toolbox:
        def compose(for_date, *a, **kw):
            self.calls.append("compose")
            out = self._compose_outputs[min(self._compose_i, len(self._compose_outputs) - 1)]
            self._compose_i += 1
            return out

        def write_notion(for_date, plan, rationale, research_used=False):
            self.calls.append("write_notion")
            self.written = (for_date, plan, rationale)

        def record(for_date, plan, rationale, research_used, tier):
            self.calls.append("record")
            self.recorded = (for_date, plan, research_used, tier)
            return 42

        return Toolbox(
            get_garmin_today=lambda d: (self.calls.append("garmin"), self._garmin)[1],
            get_notion_logs=lambda d: (self.calls.append("notion"), self._notion)[1],
            get_checkin=lambda d: (self.calls.append("checkin"), self._checkin)[1],
            query_history=lambda d: (self.calls.append("history"), self._features)[1],
            research_training=lambda q, k=5: (
                self.calls.append("research"),
                [ResearchHit(source="corpus", title="t", snippet="s")],
            )[1],
            compose_session=compose,
            write_notion=write_notion,
            record_suggestion=record,
            create_garmin_workout=lambda s: (_ for _ in ()).throw(
                AssertionError("must not push to Garmin in propose-only mode")
            ),
            schedule_workout=lambda *a: None,
        )


def test_routine_night_skips_research_and_proposes():
    rec = Recorder()
    report = run_agent(TODAY, tools=rec.toolbox())
    assert "research" not in rec.calls
    assert rec.calls == [
        "garmin", "notion", "history", "checkin", "compose", "write_notion", "record",
    ]
    assert report.suggestion_id == 42
    assert report.tier == "fast"
    assert not report.fell_back
    assert report.for_date == TOMORROW
    assert rec.written[0] == TOMORROW


def test_pain_spike_triggers_exactly_one_research_call():
    rec = Recorder(notion=NotionDay(day=TODAY, pain_level=6, pt_done=True))
    report = run_agent(TODAY, tools=rec.toolbox())
    assert rec.calls.count("research") == 1
    assert report.research_used
    assert rec.recorded[2] is True  # research_used persisted


def test_ambiguous_state_escalates_tier():
    rec = Recorder(
        garmin=GarminToday(day=TODAY, readiness=20, body_battery=10),
        notion=NotionDay(day=TODAY, pain_level=6, pt_done=True),
    )
    report = run_agent(TODAY, tools=rec.toolbox())
    assert report.tier == "quality"


def test_invalid_proposal_gets_one_revision():
    bad = sane_session(TOMORROW, steps=[ExerciseStep(exercise="Box jump", sets=3, reps=5)])
    rec = Recorder(compose_outputs=[bad, sane_session(TOMORROW)])
    report = run_agent(TODAY, tools=rec.toolbox())
    assert rec.calls.count("compose") == 2
    assert not report.fell_back
    assert report.session.title == "Upper push"


def test_double_rejection_falls_back_conservatively():
    bad = sane_session(TOMORROW, steps=[ExerciseStep(exercise="Depth jump", sets=3, reps=5)])
    rec = Recorder(compose_outputs=[bad, bad])
    report = run_agent(TODAY, tools=rec.toolbox())
    assert report.fell_back
    assert report.session.kind == "mobility"
    # the fallback still gets proposed + recorded
    assert rec.written[1].kind == "mobility"


def test_tool_budget_is_enforced():
    rec = Recorder()
    with pytest.raises(ToolBudgetExceeded):
        run_agent(TODAY, tools=rec.toolbox(), max_tool_calls=2)


def test_checkin_is_read_for_tomorrow_and_passed_to_compose():
    seen = {}

    def compose(for_date, *a, **kw):
        seen["checkin"] = kw.get("checkin")
        return sane_session(TOMORROW)

    rec = Recorder(checkin=CheckIn(for_date=TOMORROW, focus="upper", location="home", minutes=30))
    tb = rec.toolbox()
    tb.compose_session = compose
    checkin_days: list = []
    tb.get_checkin = lambda d: (checkin_days.append(d), rec._checkin)[1]
    run_agent(TODAY, tools=tb)
    assert checkin_days == [TOMORROW]  # check-in is for the target day, not today
    assert seen["checkin"].focus == "upper"
    assert seen["checkin"].location == "home"


def test_plan_for_today_targets_today_everywhere():
    # The morning re-plan runs the same loop with plan_for=today: the check-in,
    # compose target, Notion proposal, and suggestion must all be dated today.
    rec = Recorder(compose_outputs=[sane_session(TODAY)])
    tb = rec.toolbox()
    checkin_days: list = []
    tb.get_checkin = lambda d: (checkin_days.append(d), rec._checkin)[1]
    report = run_agent(TODAY, tools=tb, plan_for=TODAY)
    assert report.for_date == TODAY
    assert checkin_days == [TODAY]
    assert rec.written[0] == TODAY
    assert rec.recorded[0] == TODAY


def test_base_template_schedules_by_id_without_rebuild(monkeypatch):
    # When the agent selects a base workout (garmin_workout_id set) and auto-push
    # is on, the loop schedules the existing Garmin workout — never rebuilds it.
    import vesper.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "AUTO_PUSH", True)
    scheduled: list = []
    selected = sane_session(TOMORROW, garmin_workout_id="1414015802", template_key="full_body_b")
    rec = Recorder(compose_outputs=[selected])
    tb = rec.toolbox()
    tb.schedule_workout = lambda wid, on: scheduled.append((wid, on))
    # create_garmin_workout still raises if called — proves no rebuild happened
    run_agent(TODAY, tools=tb)
    assert scheduled == [("1414015802", TOMORROW)]
