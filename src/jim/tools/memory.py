"""Suggestion/outcome memory: what was proposed vs. what actually happened.

The morning reconcile job (jobs/reconcile.py) closes the loop by matching
Garmin actuals against the stored suggestion and writing `outcomes`."""

import json
from datetime import date

from jim.schemas import StructuredSession


def record_suggestion(
    for_date: date,
    plan: StructuredSession,
    rationale: str,
    research_used: bool,
    tier: str,
) -> int:
    from jim.db import connect

    with connect() as conn:
        row = conn.execute(
            "INSERT INTO suggestions (for_date, plan, rationale, research_used, model_tier)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (for_date, json.dumps(plan.model_dump(mode="json")), rationale, research_used, tier),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def record_outcome(
    suggestion_id: int,
    actual_activity_id: str | None,
    adhered: bool | None,
    notes: str = "",
) -> None:
    from jim.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO outcomes (suggestion_id, actual_activity_id, adhered, notes)"
            " VALUES (%s, %s, %s, %s)",
            (suggestion_id, actual_activity_id, adhered, notes),
        )
        conn.commit()
