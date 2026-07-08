"""Chat interface core: a message to the agent IS a check-in, and triggers an
immediate re-plan — no waiting for a cron.

"knee sore today, keep it light, home, 30 min" → CheckIn(note=...) →
run_agent(plan_for=<today or tomorrow>) → proposal summary replied in chat.

Target-day rule: messages before CHAT_TODAY_CUTOFF_HOUR local re-plan TODAY
(morning coffee case); later messages are a check-in for TOMORROW (they run the
plan early — the nightly cron will simply see nothing newer to change). A
leading "today"/"tomorrow" word overrides the rule.

This module is transport-agnostic: the built-in web chat (app.py /chat) calls
`handle_chat_message`, and a WhatsApp/SMS/Discord webhook route can reuse it
unchanged."""

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from jim.config import CHAT_TODAY_CUTOFF_HOUR
from jim.schemas import CheckIn, StructuredSession

log = logging.getLogger(__name__)

MAX_REPLY_CHARS = 3800  # under every transport's message limit


def resolve_target_day(text: str, now: datetime) -> tuple[date, str]:
    """(target day, note with any leading today/tomorrow keyword stripped)."""
    stripped = text.strip()
    lowered = stripped.lower()
    for keyword, offset in (("tomorrow", 1), ("today", 0)):
        if lowered.startswith(keyword):
            note = stripped[len(keyword):].lstrip(" :,-")
            return now.date() + timedelta(days=offset), note or stripped
    if now.hour < CHAT_TODAY_CUTOFF_HOUR:
        return now.date(), stripped
    return now.date() + timedelta(days=1), stripped


def format_proposal(report, today: date | None = None) -> str:
    session: StructuredSession | None = report.session
    if session is None:
        return "Something went wrong — no session was produced."
    if report.for_date == today:
        when = "today"
    elif today is not None and report.for_date == today + timedelta(days=1):
        when = "tomorrow"
    else:
        when = str(report.for_date)
    lines = [
        f"Plan for {when}: {session.title}"
        f" ({session.kind}, ~{session.est_duration_min:.0f} min)"
    ]
    if session.garmin_workout_id:
        ref = session.template_key or session.garmin_workout_id
        lines.append(f"→ scheduling your existing Garmin workout ({ref})")
    else:
        for s in session.steps[:12]:
            dose = f"{s.sets}x{s.reps}" if s.reps else f"{s.sets}x{s.duration_sec}s"
            weight = f" @ {s.weight_kg}kg" if s.weight_kg else ""
            lines.append(f"• {s.exercise} — {dose}{weight}")
        if len(session.steps) > 12:
            lines.append(f"… +{len(session.steps) - 12} more")
    if session.rationale_summary:
        lines.append(f"Why: {session.rationale_summary}")
    if report.fell_back:
        lines.append("(guardrail rejected the adapted plan twice — fell back to PT + mobility)")
    return "\n".join(lines)[:MAX_REPLY_CHARS]


def handle_chat_message(
    text: str,
    now: datetime,
    runner: Callable | None = None,
) -> str:
    """Turn one chat message into a re-plan and return the reply text."""
    if runner is None:
        from jim.agent.loop import run_agent

        runner = run_agent
    target, note = resolve_target_day(text, now)
    checkin = CheckIn(for_date=target, note=note, edited_ts=now)
    log.info("chat check-in for %s: %r", target, note[:80])
    try:
        report = runner(now.date(), plan_for=target, checkin=checkin)
    except Exception:
        log.exception("chat re-plan failed")
        return "Couldn't build a plan just now — I'll try again on the nightly run."
    return format_proposal(report, today=now.date())
