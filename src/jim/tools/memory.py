"""Planned-vs-done memory: what went on the calendar, and what happened.

Every MCP write that schedules a workout records a `suggestions` row linked
to its Garmin workout_id (record_plan); editing, unscheduling or deleting
that workout keeps the row in step (update_plan / cancel_plans). The week
overview and the nightly reconcile compare these against Garmin's actual
activities."""

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
    workout_id: str | None = None,
) -> int:
    from jim.db import connect

    with connect() as conn:
        row = conn.execute(
            "INSERT INTO suggestions (user_id, for_date, plan, rationale, research_used,"
            " model_tier, source, workout_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (user_id, for_date, json.dumps(plan.model_dump(mode="json")), rationale,
             research_used, tier, source, workout_id),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def record_plan(user_id: int, workout_id: str, plan: StructuredSession) -> int:
    """A workout the MCP server just put on the calendar for plan.for_date."""
    return record_suggestion(
        user_id, plan.for_date, plan, rationale=plan.rationale_summary,
        research_used=False, tier="claude", source="mcp", workout_id=workout_id,
    )


def update_plan(user_id: int, workout_id: str, plan: StructuredSession, from_day: date) -> None:
    """update_workout changed this workout in place: today's and future
    plans that point at it now describe the new version. Past ones keep what
    was actually planned at the time."""
    from jim.db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, for_date FROM suggestions WHERE user_id = %s AND workout_id = %s"
            " AND NOT cancelled AND for_date >= %s",
            (user_id, workout_id, from_day),
        ).fetchall()
        for r in rows:
            dated = plan.model_copy(update={"for_date": r["for_date"]})
            conn.execute(
                "UPDATE suggestions SET plan = %s WHERE id = %s",
                (json.dumps(dated.model_dump(mode="json")), r["id"]),
            )
        conn.commit()


def cancel_plans(
    user_id: int,
    workout_ids: list[str],
    on: date | None = None,
    from_day: date | None = None,
) -> int:
    """Mark plans cancelled — the athlete took the workout off the calendar
    (`on` = that one day) or deleted it (`from_day` = today onward, so past
    days keep their record). Returns how many rows changed."""
    from jim.db import connect

    if not workout_ids:
        return 0
    clauses = ["user_id = %s", "workout_id = ANY(%s)", "NOT cancelled"]
    params: list = [user_id, list(workout_ids)]
    if on is not None:
        clauses.append("for_date = %s")
        params.append(on)
    if from_day is not None:
        clauses.append("for_date >= %s")
        params.append(from_day)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE suggestions SET cancelled = true WHERE {' AND '.join(clauses)}",  # noqa: S608
            params,
        )
        conn.commit()
    return cur.rowcount or 0


def planned_between(user_id: int, start: date, end: date) -> list[dict]:
    """Live (not cancelled) plans in [start, end], newest record per
    (date, workout) — re-scheduling the same workout on a day doesn't count
    it twice."""
    from jim.db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (for_date, COALESCE(workout_id, id::text))"
            " id, for_date, plan, workout_id, source FROM suggestions"
            " WHERE user_id = %s AND for_date BETWEEN %s AND %s AND NOT cancelled"
            " ORDER BY for_date, COALESCE(workout_id, id::text), run_ts DESC",
            (user_id, start, end),
        ).fetchall()
    return list(rows)


def record_outcome(
    user_id: int,
    suggestion_id: int,
    actual_activity_id: str | None,
    adhered: bool | None,
    notes: str = "",
) -> None:
    from jim.db import connect

    with connect() as conn:
        # One outcome per plan: a re-run of the nightly reconcile (or a
        # retried cron) used to stack duplicate rows.
        existing = conn.execute(
            "SELECT 1 FROM outcomes WHERE user_id = %s AND suggestion_id = %s",
            (user_id, suggestion_id),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO outcomes (user_id, suggestion_id, actual_activity_id, adhered, notes)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, suggestion_id, actual_activity_id, adhered, notes),
        )
        conn.commit()
