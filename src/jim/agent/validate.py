"""Deterministic guardrail — runs before any Garmin write (PLAN.md §7).

Rejecting returns the violations so the agent can revise once, then fall back
to a conservative session. No LLM involvement: these are hard constraints.

There is deliberately NO weekly volume cap. Capping total minutes per week is a
crude proxy for "don't overdo it" that mostly punished normal training: because
the budget was a weekly number, checking it per-day rejected any session over
~10% of last week's total, and a full week became impossible to build. What
matters is that each day is a sane length and the work is spread over the body.

So the rules split in two:
- HARD (reject a day): session length, forbidden movements, Garmin's step cap,
  and leg-day spacing — the knee constraints.
- ADVISORY (`balance_notes`): how the plan is distributed across legs/push/pull/
  core/conditioning. An unbalanced week is suboptimal, not dangerous, and
  silently dropping days is worse than letting a skewed plan through — so this
  is fed back to the coach as guidance instead of rejecting anything.

Entrypoints: `validate` (one session, the nightly job) and `validate_plan` (a
multi-day draft, the chat coach)."""

from collections import Counter
from datetime import date, timedelta

from jim.config import (
    BALANCE_GROUPS,
    BALANCE_MAX_SHARE,
    BALANCE_MIN_SESSIONS,
    FORBIDDEN_EXERCISES,
    GARMIN_MAX_STEPS,
    MAX_SESSION_MIN,
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


def validate(session: StructuredSession, features: HistoryFeatures) -> ValidationResult:
    """One session — the nightly next-day suggestion."""
    violations = _session_violations(session)

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


# --- balance (advisory, never rejects) --------------------------------------


def session_groups(session: StructuredSession) -> dict[str, float]:
    """How one planned session's minutes split across the balance groups.

    Mobility/PT and rest contribute nothing: PT is meant to run daily and would
    otherwise dominate every split (it is ~70% of this athlete's logged volume).
    A strength day is split by the muscles its steps actually hit."""
    if session.kind in ("rest", "mobility"):
        return {}
    if session.kind == "conditioning":
        return {"conditioning": 1.0}

    counts = Counter(classify_muscle_group(step.exercise) for step in session.steps)
    for group in list(counts):
        if group not in BALANCE_GROUPS:
            del counts[group]  # unrecognised movements don't get a vote
    total = sum(counts.values())
    if not total:
        return {}
    return {group: n / total for group, n in counts.items()}


def plan_balance(sessions: list[StructuredSession]) -> dict[str, float]:
    """Share of the plan's LOADING minutes per group (legs/push/pull/core/cardio)."""
    totals: dict[str, float] = {}
    for session in sessions:
        for group, share in session_groups(session).items():
            totals[group] = totals.get(group, 0.0) + session.est_duration_min * share
    grand = sum(totals.values())
    if grand == 0:
        return {}
    return {g: round(v / grand, 3) for g, v in totals.items()}


def balance_notes(sessions: list[StructuredSession]) -> list[str]:
    """Advice on how the plan is distributed — never a rejection.

    Deliberately quiet on short plans: a single day is *supposed* to be one-sided,
    and nagging about it would just make the coach pad every session."""
    loading = [s for s in sessions if session_groups(s)]
    if len(loading) < BALANCE_MIN_SESSIONS:
        return []

    balance = plan_balance(sessions)
    notes: list[str] = []

    for group, share in sorted(balance.items(), key=lambda x: -x[1]):
        if share > BALANCE_MAX_SHARE:
            notes.append(
                f"{group} is {share:.0%} of the plan's loading work — over the"
                f" {BALANCE_MAX_SHARE:.0%} target; spread it out"
            )

    missing = [g for g in BALANCE_GROUPS if g not in balance]
    if missing:
        notes.append("nothing for " + ", ".join(missing) + " in this plan")
    return notes


def validate_plan(
    sessions: list[StructuredSession], features: HistoryFeatures
) -> dict[str, list[str]]:
    """A multi-day draft judged as a set. Returns {for_date_iso: violations}.

    Safety only. Balance is handled by `balance_notes`, which advises rather than
    rejects. Planned leg days space against each other as well as against history
    — checking only against history let two planned leg days sit back to back."""
    last_leg: date | None = (
        features.as_of - timedelta(days=features.days_since_legs)
        if features.days_since_legs is not None
        else None
    )

    results: dict[str, list[str]] = {}
    for session in sorted(sessions, key=lambda s: s.for_date):
        violations = _session_violations(session)

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
