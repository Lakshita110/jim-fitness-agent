"""Deterministic guardrail — runs before any Notion/Garmin write (PLAN.md §7).

Rejecting returns the violations so the agent can revise once, then fall back
to a conservative session. No LLM involvement: these are hard constraints.

Two entrypoints:
- `validate` judges ONE session against the trailing week (the nightly job).
- `validate_plan` judges a multi-day draft as a set (the chat coach), which is
  a different question — see its docstring."""

from datetime import date, timedelta

from jim.config import (
    FORBIDDEN_EXERCISES,
    GARMIN_MAX_STEPS,
    MAX_LOAD_PROGRESSION,
    MAX_SESSION_MIN,
    MAX_WEEKLY_VOLUME_MIN,
    MIN_DAYS_BETWEEN_LEG_SESSIONS,
)
from jim.schemas import HistoryFeatures, StructuredSession, ValidationResult
from jim.tools.history import classify_muscle_group


def _session_violations(session: StructuredSession) -> list[str]:
    """Checks that depend only on the session itself, not on history."""
    violations: list[str] = []

    # Knee/ankle constraints: no forbidden movement patterns.
    for step in session.steps:
        lowered = step.exercise.lower()
        for banned in FORBIDDEN_EXERCISES:
            if banned in lowered:
                violations.append(f"forbidden exercise for knee/ankle: '{step.exercise}'")
                break

    # Garmin hard limit on workout steps.
    if len(session.steps) > GARMIN_MAX_STEPS:
        violations.append(f"{len(session.steps)} steps exceeds Garmin max {GARMIN_MAX_STEPS}")

    # Session duration sanity.
    if session.est_duration_min > MAX_SESSION_MIN:
        violations.append(
            f"session {session.est_duration_min:.0f} min exceeds max {MAX_SESSION_MIN}"
        )
    return violations


def is_leg_session(session: StructuredSession) -> bool:
    """Loading sessions only — PT/mobility leg work is meant to happen daily."""
    return session.kind in ("strength", "conditioning") and any(
        classify_muscle_group(step.exercise) == "legs" for step in session.steps
    )


def weekly_budget(features: HistoryFeatures) -> float:
    """Minutes the coming week may total: last week +10%, under the hard ceiling.

    With no trailing history there is nothing to progress from, so only the
    ceiling applies (a 0-minute baseline would otherwise budget 30 min/week and
    block every plan)."""
    if features.weekly_volume_min <= 0:
        return float(MAX_WEEKLY_VOLUME_MIN)
    progression = features.weekly_volume_min * (1 + MAX_LOAD_PROGRESSION) + 30
    return min(progression, float(MAX_WEEKLY_VOLUME_MIN))


def validate(session: StructuredSession, features: HistoryFeatures) -> ValidationResult:
    """One session against the trailing week — the nightly next-day suggestion."""
    violations = _session_violations(session)

    # Weekly volume in bounds, including sane week-over-week progression.
    projected = features.weekly_volume_min + session.est_duration_min
    if projected > MAX_WEEKLY_VOLUME_MIN:
        violations.append(
            f"projected weekly volume {projected:.0f} min exceeds cap {MAX_WEEKLY_VOLUME_MIN}"
        )
    allowed = features.weekly_volume_min * (1 + MAX_LOAD_PROGRESSION) + 30
    if features.weekly_volume_min > 0 and projected > allowed:
        violations.append(
            f"progression too steep: projected {projected:.0f} min vs allowed {allowed:.0f}"
        )

    if (
        is_leg_session(session)
        and features.days_since_legs is not None
        and features.days_since_legs < MIN_DAYS_BETWEEN_LEG_SESSIONS
    ):
        violations.append(
            f"leg session only {features.days_since_legs} day(s) after the last one"
            f" (minimum {MIN_DAYS_BETWEEN_LEG_SESSIONS})"
        )

    return ValidationResult(ok=not violations, violations=violations)


def validate_plan(
    sessions: list[StructuredSession], features: HistoryFeatures
) -> dict[str, list[str]]:
    """A multi-day draft judged as a set. Returns {for_date_iso: violations}.

    Running the single-session `validate` once per day is wrong in both
    directions. The week-over-week progression budget is a *weekly* allowance,
    but per-day it gets re-tested as "last week's total + this one day" — so
    every individual day has to fit inside last week's 10% headroom, which
    rejects any normal session and makes a full week impossible to build.
    Meanwhile the planned days never accumulate against each other, so a week of
    seven 90-minute sessions passes untouched.

    Here one weekly budget is spent across the plan in date order, and planned
    leg days space against each other as well as against history."""
    budget = weekly_budget(features)
    last_leg: date | None = (
        features.as_of - timedelta(days=features.days_since_legs)
        if features.days_since_legs is not None
        else None
    )

    results: dict[str, list[str]] = {}
    spent = 0.0
    for session in sorted(sessions, key=lambda s: s.for_date):
        violations = _session_violations(session)

        spent += session.est_duration_min
        if spent > budget:
            violations.append(
                f"weekly volume budget exceeded: plan totals {spent:.0f} min"
                f" vs allowed {budget:.0f}"
            )

        if is_leg_session(session):
            if last_leg is not None:
                gap = (session.for_date - last_leg).days
                if gap < MIN_DAYS_BETWEEN_LEG_SESSIONS:
                    violations.append(
                        f"leg session only {gap} day(s) after the last one"
                        f" (minimum {MIN_DAYS_BETWEEN_LEG_SESSIONS})"
                    )
            last_leg = session.for_date

        if violations:
            results[session.for_date.isoformat()] = violations
    return results


def fallback_session(session: StructuredSession) -> StructuredSession:
    """Conservative fallback when revision still fails: PT + mobility only."""
    from jim.schemas import ExerciseStep

    return StructuredSession(
        for_date=session.for_date,
        kind="mobility",
        title="Fallback: PT protocol + mobility",
        steps=[
            ExerciseStep(exercise="PT protocol (full)", sets=1, duration_sec=1200),
            ExerciseStep(exercise="Hip mobility flow", sets=1, duration_sec=600),
        ],
        est_duration_min=30,
        rationale_summary="Proposed session failed validation twice; defaulting to PT + mobility.",
    )
