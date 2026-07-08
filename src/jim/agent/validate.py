"""Deterministic guardrail — runs before any Notion/Garmin write (PLAN.md §7).

Rejecting returns the violations so the agent can revise once, then fall back
to a conservative session. No LLM involvement: these are hard constraints."""

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


def validate(session: StructuredSession, features: HistoryFeatures) -> ValidationResult:
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

    # Leg-day spacing — only for loading sessions; PT/mobility is meant daily.
    is_leg_session = session.kind in ("strength", "conditioning") and any(
        classify_muscle_group(step.exercise) == "legs" for step in session.steps
    )
    if (
        is_leg_session
        and features.days_since_legs is not None
        and features.days_since_legs < MIN_DAYS_BETWEEN_LEG_SESSIONS
    ):
        violations.append(
            f"leg session only {features.days_since_legs} day(s) after the last one"
            f" (minimum {MIN_DAYS_BETWEEN_LEG_SESSIONS})"
        )

    return ValidationResult(ok=not violations, violations=violations)


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
