"""Suggestion/outcome memory: what was proposed vs. what actually happened.

The morning reconcile job (jobs/reconcile.py) closes the loop by matching
Garmin actuals against the stored suggestion and writing `outcomes`."""

import json
from datetime import date

from jim.schemas import StructuredSession


def record_suggestion(
    user_id: int,
    for_date: date,
    plan: StructuredSession,
    rationale: str,
    research_used: bool,
    tier: str,
    source: str = "nightly",
) -> int:
    from jim.db import connect

    with connect() as conn:
        row = conn.execute(
            "INSERT INTO suggestions (user_id, for_date, plan, rationale, research_used,"
            " model_tier, source) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, for_date, json.dumps(plan.model_dump(mode="json")), rationale,
             research_used, tier, source),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def chat_planned(user_id: int, for_date: date) -> bool:
    """True when the athlete already iterated + approved a plan for `for_date`
    in chat — the nightly run must not overwrite it."""
    from jim.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM suggestions WHERE user_id = %s AND for_date = %s"
            " AND source = 'chat' LIMIT 1",
            (user_id, for_date),
        ).fetchone()
    return row is not None


def record_outcome(
    user_id: int,
    suggestion_id: int,
    actual_activity_id: str | None,
    adhered: bool | None,
    notes: str = "",
) -> None:
    from jim.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO outcomes (user_id, suggestion_id, actual_activity_id, adhered, notes)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, suggestion_id, actual_activity_id, adhered, notes),
        )
        conn.commit()
