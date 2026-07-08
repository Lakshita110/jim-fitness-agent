"""Reconciliation (`reconcile_day`, used by the nightly job) + an OPTIONAL
morning job. `reconcile_day` matches a day's Garmin actuals against its stored
suggestion, writing `outcomes` (adherence). The optional `main` additionally
re-plans today when a Notion check-in was written after the nightly proposal —
only needed if you use Notion morning check-ins instead of the chat interface.
`python -m jim.jobs.reconcile`."""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jim.config import settings
from jim.db import connect
from jim.schemas import ActivitySummary, CheckIn, StructuredSession
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


def reconcile_day(day: date) -> None:
    """Match `day`'s Garmin actuals against its stored suggestion → outcomes.
    The nightly job calls this for *today* (the session is done by 21:00)."""
    from jim.tools.garmin import get_garmin_today

    with connect() as conn:
        row = conn.execute(
            "SELECT id, plan FROM suggestions WHERE for_date = %s ORDER BY run_ts DESC LIMIT 1",
            (day,),
        ).fetchone()
    if row is None:
        log.info("no suggestion stored for %s; nothing to reconcile", day)
        return

    plan = StructuredSession.model_validate(row["plan"])
    actuals = get_garmin_today(day).activities
    ok, notes = adhered(plan, actuals)
    record_outcome(
        suggestion_id=row["id"],
        actual_activity_id=actuals[0].activity_id if actuals else None,
        adhered=ok,
        notes=notes,
    )
    log.info("reconciled %s: adhered=%s (%s)", day, ok, notes)


# --- morning re-plan: honor a check-in written after last night's proposal ---


def needs_replan(checkin: CheckIn, last_run_ts: datetime | None) -> bool:
    """True when today's check-in should trigger a same-day re-plan.

    Re-plan when a non-empty check-in exists and either no proposal was made
    for today, or the check-in was created/edited after that proposal."""
    if checkin.is_empty():
        return False
    if last_run_ts is None:
        return True
    return checkin.edited_ts is not None and checkin.edited_ts > last_run_ts


def morning_replan(today: date) -> None:
    from jim.agent.loop import run_agent
    from jim.config import AUTO_PUSH
    from jim.tools.notion import get_checkin

    checkin = get_checkin(today)
    with connect() as conn:
        row = conn.execute(
            "SELECT run_ts FROM suggestions WHERE for_date = %s ORDER BY run_ts DESC LIMIT 1",
            (today,),
        ).fetchone()
    last_run_ts = row["run_ts"] if row else None

    if not needs_replan(checkin, last_run_ts):
        log.info("no fresh check-in for %s; keeping last night's plan", today)
        return

    log.info("check-in for %s arrived after the nightly proposal — re-planning", today)
    if AUTO_PUSH:
        # Drop the stale auto-pushed schedule before the re-plan pushes its own.
        from jim.tools.garmin import clear_schedule

        clear_schedule(today)
    report = run_agent(today, plan_for=today)
    log.info("morning re-plan done: %s", report)


def main() -> None:
    """OPTIONAL morning job. Not scheduled by default — reconciliation happens
    in the nightly run, and same-day changes come through the chat interface.
    Schedule this only if you prefer Notion morning check-ins over chat."""
    logging.basicConfig(level=logging.INFO)
    today = datetime.now(ZoneInfo(settings().app_timezone)).date()
    reconcile_day(today - timedelta(days=1))
    morning_replan(today)


if __name__ == "__main__":
    main()
