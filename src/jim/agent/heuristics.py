"""Cheap deterministic heuristics that gate the expensive paths:

- `something_off` gates the research tool (PLAN.md §4): research only runs
  when state deviates from routine.
- `state_ambiguous` gates cheap→quality tier escalation.

Both are pure functions of the day's state — no LLM, no I/O."""

from jim.config import MIN_DAYS_BETWEEN_LEG_SESSIONS
from jim.schemas import GarminToday, HistoryFeatures, NotionDay

PAIN_SPIKE_LEVEL = 5  # absolute pain level that always counts as "off"
PAIN_TREND_ALERT = 0.15  # points/day upward slope over the window
LOW_READINESS = 35
LOW_BODY_BATTERY = 20


def something_off(
    garmin: GarminToday, notion: NotionDay, features: HistoryFeatures
) -> list[str]:
    """Returns the list of reasons state is 'off' (empty = routine night)."""
    reasons: list[str] = []
    if notion.pain_level is not None and notion.pain_level >= PAIN_SPIKE_LEVEL:
        reasons.append(f"pain spike: level {notion.pain_level} ({notion.pain_location})")
    if features.pain_trend > PAIN_TREND_ALERT:
        reasons.append(f"pain trending up ({features.pain_trend:+.2f}/day)")
    if garmin.readiness is not None and garmin.readiness < LOW_READINESS:
        reasons.append(f"low readiness ({garmin.readiness})")
    if garmin.body_battery is not None and garmin.body_battery < LOW_BODY_BATTERY:
        reasons.append(f"low body battery ({garmin.body_battery})")
    if not notion.pt_done and notion.pain_level is not None and notion.pain_level >= 3:
        reasons.append("PT skipped on a painful day")
    return reasons


def state_ambiguous(off_reasons: list[str], features: HistoryFeatures) -> bool:
    """Escalate to the quality tier only when signals conflict or pile up."""
    if len(off_reasons) >= 2:
        return True
    # Conflicting signals: pain worsening but a leg session is overdue.
    legs_overdue = (
        features.days_since_legs is not None
        and features.days_since_legs > MIN_DAYS_BETWEEN_LEG_SESSIONS * 2
    )
    return bool(off_reasons) and legs_overdue
