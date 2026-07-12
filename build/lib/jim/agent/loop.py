"""`run_agent` — the bounded nightly run (PLAN.md §4 control flow).

Single-shot, hard-capped on tool calls. Deterministic Python decides *whether*
to research and *which* tier to use; the LLM is only generative at the compose
step. Propose-only: the proposal lands as the chat DRAFT (kv) for review in
Jim's chat — AUTO_PUSH gates any unattended Garmin write behind M5 evals.
If the athlete already planned the target date in chat, the nightly run
steps aside.

Tools are injected so the loop unit-tests with fakes (no live APIs in CI)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from jim.agent import compose, heuristics, validate
from jim.config import (
    AUTO_PUSH,
    MAX_TOOL_CALLS,
    MODEL_FAST,
    MODEL_QUALITY,
    RESEARCH_ENABLED,
)
from jim.schemas import ResearchHit, StructuredSession

if TYPE_CHECKING:
    from jim.playbook import Playbook

log = logging.getLogger(__name__)


class ToolBudgetExceeded(RuntimeError):
    pass


@dataclass
class Toolbox:
    """Injection point for every side-effecting dependency of the loop."""

    get_garmin_today: Callable[..., Any]
    get_notion_logs: Callable[..., Any]
    query_history: Callable[..., Any]
    research_training: Callable[..., list[ResearchHit]]
    compose_session: Callable[..., StructuredSession]
    save_draft: Callable[..., None]  # proposal -> chat draft (kv)
    record_suggestion: Callable[..., int]
    chat_planned: Callable[..., bool]  # date already planned via chat?
    load_goals: Callable[[], str]  # long-term goals block (kv)
    create_garmin_workout: Callable[..., Any]
    schedule_workout: Callable[..., None]

    @classmethod
    def live(cls) -> "Toolbox":
        from jim.db import kv_get, kv_set
        from jim.tools import garmin, history, memory, notion, research

        return cls(
            get_garmin_today=garmin.get_garmin_today,
            get_notion_logs=notion.get_notion_logs,
            query_history=history.query_history,
            research_training=research.research_training,
            compose_session=compose.compose_session,
            save_draft=lambda sessions: kv_set("draft", sessions),
            record_suggestion=memory.record_suggestion,
            chat_planned=memory.chat_planned,
            load_goals=lambda: kv_get("goals") or "",
            create_garmin_workout=garmin.create_garmin_workout,
            schedule_workout=garmin.schedule_workout,
        )


@dataclass
class RunReport:
    for_date: date
    suggestion_id: int | None = None
    session: StructuredSession | None = None
    tier: str = "fast"
    research_used: bool = False
    off_reasons: list[str] = field(default_factory=list)
    tool_calls: int = 0
    fell_back: bool = False
    skipped: bool = False  # target date was already planned in chat


def run_agent(
    today: date,
    tools: Toolbox | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
    playbook: "Playbook | None" = None,
    plan_for: date | None = None,
) -> RunReport:
    """Plan the session for `plan_for` (default: tomorrow — the nightly run)."""
    from jim.playbook import load_playbook

    tools = tools or Toolbox.live()
    playbook = playbook if playbook is not None else load_playbook()
    target = plan_for or (today + timedelta(days=1))
    report = RunReport(for_date=target)

    calls = 0

    def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > max_tool_calls:
            raise ToolBudgetExceeded(f"exceeded {max_tool_calls} tool calls")
        report.tool_calls = calls
        return fn(*args, **kwargs)

    # 0. The athlete's own chat-approved plan always wins.
    if call(tools.chat_planned, target):
        log.info("%s already planned via chat — nightly run steps aside", target)
        report.skipped = True
        return report

    # 1-3. Reads: everything the model will see, as compact summaries.
    garmin_today = call(tools.get_garmin_today, today)
    notion_day = call(tools.get_notion_logs, today)
    features = call(tools.query_history, today)
    goals_text = call(tools.load_goals)

    # 4. Gated research: only when the deterministic heuristic says something's off.
    report.off_reasons = heuristics.something_off(garmin_today, notion_day, features)
    research: list[ResearchHit] = []
    if RESEARCH_ENABLED and report.off_reasons:
        question = (
            "Training adjustment for the next session given: " + "; ".join(report.off_reasons)
        )
        research = call(tools.research_training, question)
        report.research_used = True

    # Tier escalation only on ambiguous state.
    model = MODEL_FAST
    if heuristics.state_ambiguous(report.off_reasons, features):
        model = MODEL_QUALITY
        report.tier = "quality"
    log.info("composing on %s (off: %s)", model, report.off_reasons or "nothing")

    # 5-6. Compose (playbook + goals in context), guardrail + one revision, fallback.
    playbook_text = playbook.to_prompt()
    session = call(
        tools.compose_session, target, garmin_today, notion_day, features, research,
        model=model, playbook_text=playbook_text, goals_text=goals_text,
    )
    result = validate.validate(session, features)
    if not result.ok:
        log.warning("proposal rejected: %s — revising once", result.violations)
        session = call(
            tools.compose_session, target, garmin_today, notion_day, features,
            research, model=model, revision_feedback=result.violations,
            playbook_text=playbook_text, goals_text=goals_text,
        )
        result = validate.validate(session, features)
    if not result.ok:
        log.error("revision rejected too: %s — using fallback", result.violations)
        session = validate.fallback_session(session)
        report.fell_back = True
    report.session = session

    # 7-8. Propose as the chat draft + remember what we suggested.
    rationale = session.rationale_summary
    if report.off_reasons:
        rationale += f" [flags: {'; '.join(report.off_reasons)}]"
    call(tools.save_draft, [session.model_dump(mode="json")])
    report.suggestion_id = call(
        tools.record_suggestion, target, session, rationale,
        report.research_used, report.tier,
    )

    # Unattended auto-push stays off until the M5 eval suite gates it green.
    if AUTO_PUSH and session.kind != "rest" and not report.fell_back:
        if session.garmin_workout_id:
            # Base template selected unchanged: schedule the existing Garmin
            # workout by ID (keeps loaded weights) — no rebuild.
            call(tools.schedule_workout, session.garmin_workout_id, target)
        else:
            ref = call(tools.create_garmin_workout, session)
            call(tools.schedule_workout, ref.workout_id, target)

    log.info(
        "run complete: suggestion %s for %s (%s tool calls, tier=%s)",
        report.suggestion_id, target, report.tool_calls, report.tier,
    )
    return report
