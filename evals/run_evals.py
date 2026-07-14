"""M5 groundwork — agent evals on a fixed scenario set (PLAN.md §9).

Grades three axes per scenario, with fakes (no live APIs):
- plan quality: the proposal passes the deterministic validator, no fallback
- tool-use correctness: research called exactly when the scenario expects
- trajectory cost: tool calls under MAX_TOOL_CALLS

Exit code is non-zero if any scenario fails — wire into CI to gate AUTO_PUSH.

    python evals/run_evals.py
"""

import sys
from dataclasses import dataclass
from datetime import date

from jim.agent.loop import RunReport, Toolbox, run_agent
from jim.config import MAX_TOOL_CALLS
from jim.playbook import Playbook
from jim.schemas import (
    ExerciseStep,
    GarminToday,
    HistoryFeatures,
    NotionDay,
    ResearchHit,
    StructuredSession,
)

TODAY = date(2026, 7, 6)


def sane_session(for_date: date, **overrides) -> StructuredSession:
    base = StructuredSession(
        for_date=for_date,
        kind="strength",
        title="Upper push + PT",
        steps=[
            ExerciseStep(exercise="Bench press", sets=3, reps=8, weight_kg=60),
            ExerciseStep(exercise="Overhead press", sets=3, reps=8, weight_kg=30),
            ExerciseStep(exercise="PT protocol", sets=1, duration_sec=900),
        ],
        est_duration_min=45,
        rationale_summary="Push day; legs recovered only 1 day, pain stable.",
    )
    return base.model_copy(update=overrides)


@dataclass
class Scenario:
    name: str
    garmin: GarminToday
    notion: NotionDay
    features: HistoryFeatures
    expect_research: bool


SCENARIOS = [
    Scenario(
        name="routine night — research must be skipped",
        garmin=GarminToday(day=TODAY, readiness=70, body_battery=60, sleep_hours=7.5),
        notion=NotionDay(day=TODAY, pain_level=1, pt_done=True),
        features=HistoryFeatures(
            as_of=TODAY, window_days=28, weekly_volume_min=200, days_since_legs=1
        ),
        expect_research=False,
    ),
    Scenario(
        name="pain spike — exactly one research call",
        garmin=GarminToday(day=TODAY, readiness=65, body_battery=55),
        notion=NotionDay(day=TODAY, pain_level=6, pain_location="left knee", pt_done=True),
        features=HistoryFeatures(
            as_of=TODAY, window_days=28, weekly_volume_min=200, days_since_legs=1
        ),
        expect_research=True,
    ),
    Scenario(
        name="low readiness — research, easy day",
        garmin=GarminToday(day=TODAY, readiness=25, body_battery=30, sleep_hours=5.0),
        notion=NotionDay(day=TODAY, pain_level=2, pt_done=True),
        features=HistoryFeatures(
            as_of=TODAY, window_days=28, weekly_volume_min=250, days_since_legs=2
        ),
        expect_research=True,
    ),
]


def run_scenario(sc: Scenario) -> tuple[RunReport, list[str]]:
    research_calls = 0

    def fake_research(question: str, k: int = 5) -> list[ResearchHit]:
        nonlocal research_calls
        research_calls += 1
        return [ResearchHit(source="corpus", title="PT protocol", snippet="reduce load")]

    tools = Toolbox(
        get_garmin_today=lambda day: sc.garmin,
        get_notion_logs=lambda day: sc.notion,
        query_history=lambda day: sc.features,
        research_training=fake_research,
        compose_session=lambda for_date, *a, **kw: sane_session(for_date),
        save_draft=lambda sessions: None,
        record_suggestion=lambda *a, **kw: 1,
        chat_planned=lambda day: False,
        load_goals=lambda: "",
        create_garmin_workout=lambda s: None,
        schedule_workout=lambda *a, **kw: None,
    )
    report = run_agent(1, TODAY, tools=tools, playbook=Playbook())

    failures: list[str] = []
    if report.fell_back:
        failures.append("plan quality: fell back to conservative session")
    if sc.expect_research and research_calls != 1:
        failures.append(f"tool use: expected exactly 1 research call, got {research_calls}")
    if not sc.expect_research and research_calls != 0:
        failures.append(f"tool use: research called {research_calls}x on a routine night")
    if report.tool_calls > MAX_TOOL_CALLS:
        failures.append(f"cost: {report.tool_calls} tool calls exceeds {MAX_TOOL_CALLS}")
    return report, failures


def main() -> int:
    failed = 0
    for sc in SCENARIOS:
        report, failures = run_scenario(sc)
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {sc.name} (tool_calls={report.tool_calls}, tier={report.tier})")
        for f in failures:
            print(f"        - {f}")
        failed += bool(failures)
    print(f"\n{len(SCENARIOS) - failed}/{len(SCENARIOS)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
