"""Reconciliation: `reconcile_day` (called by the nightly job) matches a
day's Garmin actuals against its stored suggestion, writing `outcomes`
(adherence) — the loop-closer that feeds the last-7 summary."""

import logging
from datetime import date

from jim.db import connect
from jim.schemas import ActivitySummary, StructuredSession
from jim.tools.memory import record_outcome

log = logging.getLogger(__name__)

ADHERENCE_DURATION_TOLERANCE = 0.5  # actual within ±50% of proposed duration

KIND_TO_ACTIVITY_TYPES = {
    "strength": ("strength_training", "fitness_equipment"),
    "conditioning": ("running", "cycling", "walking", "cardio", "elliptical", "swimming"),
    "mobility": ("yoga", "stretching", "breathwork", "other"),
}


def adhered(plan: StructuredSession, actuals: list[ActivitySummary]) -> tuple[bool, str]:
    """Deterministic adherence check: right kind of activity, plausible duration."""
    if plan.kind == "rest":
        return (not actuals, "rest day" + (" violated" if actuals else " respected"))
    expected_types = KIND_TO_ACTIVITY_TYPES.get(plan.kind, ())
    matches = [a for a in actuals if a.type in expected_types]
    if not matches:
        return False, f"no {plan.kind} activity recorded"
    total = sum(a.duration_min for a in matches)
    lo = plan.est_duration_min * (1 - ADHERENCE_DURATION_TOLERANCE)
    hi = plan.est_duration_min * (1 + ADHERENCE_DURATION_TOLERANCE)
    if plan.est_duration_min and not (lo <= total <= hi):
        return False, f"duration off: {total:.0f} min vs proposed {plan.est_duration_min:.0f}"
    return True, f"matched {len(matches)} activity(ies), {total:.0f} min"


def reconcile_day(user_id: int, day: date) -> None:
    """Match `day`'s Garmin actuals against its stored suggestion → outcomes.
    The nightly job calls this for *today* (the session is done by 21:00)."""
    from jim.tools.garmin import get_garmin_today

    with connect() as conn:
        row = conn.execute(
            "SELECT id, plan FROM suggestions WHERE user_id = %s AND for_date = %s"
            " ORDER BY run_ts DESC LIMIT 1",
            (user_id, day),
        ).fetchone()
    if row is None:
        log.info("no suggestion stored for %s; nothing to reconcile", day)
        return

    plan = StructuredSession.model_validate(row["plan"])
    actuals = get_garmin_today(user_id, day).activities
    ok, notes = adhered(plan, actuals)
    record_outcome(
        user_id=user_id,
        suggestion_id=row["id"],
        actual_activity_id=actuals[0].activity_id if actuals else None,
        adhered=ok,
        notes=notes,
    )
    log.info("reconciled %s: adhered=%s (%s)", day, ok, notes)
