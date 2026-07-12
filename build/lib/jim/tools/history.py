"""`query_history` — deterministic features over the trailing window.

All computation is pure Python over plain dict rows so it unit-tests without a
database; `query_history` is the thin DB-backed wrapper the agent calls."""

from datetime import date, timedelta
from typing import Any

from jim.schemas import HistoryFeatures, ReadinessRead

# Activity/exercise name fragments -> coarse muscle group. Deliberately coarse:
# the balance feature only needs to expose "you haven't touched X in a while".
MUSCLE_GROUPS: dict[str, tuple[str, ...]] = {
    "legs": ("squat", "lunge", "leg", "deadlift", "hip", "calf", "glute", "step-up"),
    "push": ("bench", "press", "push", "dip", "tricep"),
    "pull": ("row", "pull", "chin", "curl", "lat"),
    "core": ("core", "plank", "ab", "carry"),
    "conditioning": ("run", "bike", "cycling", "row_erg", "swim", "cardio", "walk", "elliptical"),
}


def classify_muscle_group(name: str) -> str:
    lowered = name.lower()
    for group, needles in MUSCLE_GROUPS.items():
        if any(n in lowered for n in needles):
            return group
    return "other"


def weekly_volume_min(activities: list[dict[str, Any]], as_of: date) -> float:
    """Total training minutes in the 7 days ending at `as_of` (inclusive)."""
    start = as_of - timedelta(days=6)
    return sum(
        float(a.get("duration_min") or 0)
        for a in activities
        if start <= a["day"] <= as_of
    )


def muscle_group_balance(activities: list[dict[str, Any]], as_of: date) -> dict[str, float]:
    """Fraction of the last 7 days' volume per muscle group."""
    start = as_of - timedelta(days=6)
    totals: dict[str, float] = {}
    for a in activities:
        if not (start <= a["day"] <= as_of):
            continue
        group = classify_muscle_group(str(a.get("type", "")))
        totals[group] = totals.get(group, 0.0) + float(a.get("duration_min") or 0)
    grand = sum(totals.values())
    if grand == 0:
        return {}
    return {g: round(v / grand, 3) for g, v in totals.items()}


def days_since_legs(activities: list[dict[str, Any]], as_of: date) -> int | None:
    leg_days = [
        a["day"]
        for a in activities
        if classify_muscle_group(str(a.get("type", ""))) == "legs" and a["day"] <= as_of
    ]
    if not leg_days:
        return None
    return (as_of - max(leg_days)).days


def pain_trend(logs: list[dict[str, Any]]) -> float:
    """Least-squares slope of pain_level over the window (points/day).

    Positive = worsening. Rows without a pain_level are skipped."""
    points = [
        (log["day"].toordinal(), float(log["pain_level"]))
        for log in logs
        if log.get("pain_level") is not None
    ]
    if len(points) < 2:
        return 0.0
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denom


MAX_PAIN_NOTES = 6


def recent_pain_notes(logs: list[dict[str, Any]], limit: int = MAX_PAIN_NOTES) -> list[str]:
    """The words behind the pain trend: newest-first dated notes, e.g.
    "2026-07-11 (right, 3/10): might've been triggered by driving".

    A slope can't tell you the same complaint recurred three days running; the
    notes can. Days with no note are skipped."""
    noted = [log for log in logs if (log.get("pain_notes") or "").strip()]
    noted.sort(key=lambda log: log["day"], reverse=True)
    lines = []
    for log in noted[:limit]:
        bits = [b for b in (log.get("pain_location") or "",
                            f"{log['pain_level']}/10" if log.get("pain_level") is not None else "")
                if b]
        where = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"{log['day']}{where}: {log['pain_notes'].strip()}")
    return lines


def compute_features(
    as_of: date,
    window_days: int,
    activities: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    daily: list[dict[str, Any]],
) -> HistoryFeatures:
    """Pure assembly of all deterministic features from pre-fetched rows."""
    readiness = [d["readiness"] for d in daily if d.get("readiness") is not None]
    return HistoryFeatures(
        as_of=as_of,
        window_days=window_days,
        weekly_volume_min=weekly_volume_min(activities, as_of),
        muscle_group_balance=muscle_group_balance(activities, as_of),
        days_since_legs=days_since_legs(activities, as_of),
        pain_trend=pain_trend(logs),
        recent_pain_notes=recent_pain_notes(logs),
        avg_readiness=(sum(readiness) / len(readiness)) if readiness else None,
    )


def query_history(as_of: date, window_days: int = 28) -> HistoryFeatures:
    """DB-backed tool contract (PLAN.md §7). Fetches window rows, delegates to
    the pure functions above."""
    from jim.db import connect

    start = as_of - timedelta(days=window_days - 1)
    with connect() as conn:
        activities = conn.execute(
            "SELECT day, type, duration_min FROM garmin_activities"
            " WHERE day BETWEEN %s AND %s",
            (start, as_of),
        ).fetchall()
        logs = conn.execute(
            "SELECT day, pain_level, pain_location, pain_notes FROM notion_daily_log"
            " WHERE day BETWEEN %s AND %s",
            (start, as_of),
        ).fetchall()
        daily = conn.execute(
            "SELECT day, readiness FROM garmin_daily WHERE day BETWEEN %s AND %s",
            (start, as_of),
        ).fetchall()
    return compute_features(as_of, window_days, activities, logs, daily)


# --- load & readiness (a planning verdict, not a dashboard) -----------------

# Readiness thresholds (Garmin Training Readiness / Body Battery, 0-100).
_R_LOW = 35  # below this: recovery is poor — rest/PT
_R_MEH = 50  # below this: don't add load

# ACWR bands: <0.8 room to build, 0.8-1.3 sweet spot, >1.5 injury-risk spike.
_ACWR_LOW = 0.8
_ACWR_HIGH = 1.5


def _acwr(
    activities: list[dict[str, Any]], as_of: date, key: str
) -> tuple[float, float, float | None]:
    """Acute (7d sum), chronic (28d sum / 4 = avg week), and their ratio for `key`."""
    acute_start = as_of - timedelta(days=6)
    chronic_start = as_of - timedelta(days=27)
    acute = sum(
        float(a.get(key) or 0) for a in activities if acute_start <= a["day"] <= as_of
    )
    chronic_total = sum(
        float(a.get(key) or 0) for a in activities if chronic_start <= a["day"] <= as_of
    )
    chronic = chronic_total / 4.0
    acwr = round(acute / chronic, 2) if chronic > 0 else None
    return acute, chronic, acwr


def compute_readiness(
    as_of: date,
    activities: list[dict[str, Any]],
    daily: list[dict[str, Any]],
) -> ReadinessRead:
    """Distil trailing load + today's recovery into one planning verdict. Pure.

    Prefers Garmin training-load for the ACWR; falls back to training minutes
    when load isn't populated (strength sessions often lack a load score).
    A poor recovery read pulls the verdict down regardless of the ratio."""
    a_load, c_load, acwr_load = _acwr(activities, as_of, "training_load")
    if c_load > 0:
        acute, chronic, acwr, basis = a_load, c_load, acwr_load, "load"
    else:
        acute, chronic, acwr = _acwr(activities, as_of, "duration_min")
        basis = "minutes" if chronic > 0 else "none"

    today = next((d for d in daily if d["day"] == as_of), None) or {}
    readiness = today.get("readiness")
    body_battery = today.get("body_battery")
    recovery = readiness if readiness is not None else body_battery

    status = "steady"
    if acwr is not None:
        if acwr > _ACWR_HIGH:
            status = "ease"
        elif acwr < _ACWR_LOW:
            status = "push"
    if recovery is not None:
        if recovery < _R_LOW:
            status = "rest" if status == "ease" else "ease"
        elif recovery < _R_MEH and status == "push":
            status = "steady"

    headline = {
        "push": "Clear to push",
        "steady": "Steady — hold your load",
        "ease": "Ease off today",
        "rest": "Recovery low — rest or PT",
    }[status]

    reasons = []
    if acwr is not None:
        reasons.append(f"7d {basis} {acute:.0f} vs avg week {chronic:.0f} (ACWR {acwr})")
    label = "readiness" if readiness is not None else "body battery"
    if recovery is not None:
        reasons.append(f"{label} {recovery}")
    detail = "; ".join(reasons) or "not enough recent data yet"

    return ReadinessRead(
        as_of=as_of,
        acute_load=round(acute, 1),
        chronic_load=round(chronic, 1),
        acwr=acwr,
        basis=basis,
        readiness=readiness,
        body_battery=body_battery,
        hrv=today.get("hrv"),
        sleep_hours=today.get("sleep_hours"),
        status=status,
        headline=headline,
        detail=detail,
    )


def readiness_read(as_of: date) -> ReadinessRead:
    """DB-backed load + readiness verdict for the coach and UI badge."""
    from jim.db import connect

    start = as_of - timedelta(days=27)
    with connect() as conn:
        activities = conn.execute(
            "SELECT day, duration_min, training_load FROM garmin_activities"
            " WHERE day BETWEEN %s AND %s",
            (start, as_of),
        ).fetchall()
        daily = conn.execute(
            "SELECT day, readiness, body_battery, hrv, sleep_hours FROM garmin_daily"
            " WHERE day BETWEEN %s AND %s",
            (start, as_of),
        ).fetchall()
    return compute_readiness(as_of, activities, daily)


# --- exercise-level performance history (progression memory) ----------------


def summarize_exercise_history(rows: list[dict[str, Any]], max_sessions: int = 5) -> str:
    """Compact per-exercise performance text from exercise_sets rows.

    Pure. Rows: {day, category, exercise_name, reps, weight_kg}. Groups by
    exercise (name, falling back to category), then by day; each day renders
    as "N sets x rep-range @ max-weight". Newest sessions first."""
    by_exercise: dict[str, dict[date, list[dict[str, Any]]]] = {}
    for r in rows:
        label = r.get("exercise_name") or r.get("category") or "UNKNOWN"
        by_exercise.setdefault(label, {}).setdefault(r["day"], []).append(r)

    lines: list[str] = []
    for label in sorted(by_exercise):
        days = sorted(by_exercise[label], reverse=True)[:max_sessions]
        sessions = []
        for d in days:
            sets = by_exercise[label][d]
            reps = [s["reps"] for s in sets if s.get("reps")]
            weights = [s["weight_kg"] for s in sets if s.get("weight_kg")]
            rep_txt = (
                f"{min(reps)}-{max(reps)}" if reps and min(reps) != max(reps)
                else (str(reps[0]) if reps else "?")
            )
            weight_txt = f" @ {max(weights):g}kg" if weights else ""
            sessions.append(f"{d.isoformat()}: {len(sets)}x{rep_txt}{weight_txt}")
        lines.append(f"{label}: " + "; ".join(sessions))
    return "\n".join(lines) if lines else "(no logged sets found)"


def exercise_history(exercise: str, days: int = 180) -> str:
    """How the athlete actually performed a movement recently (DB-backed tool).

    Fuzzy match: "goblet squat" hits GOBLET_SQUAT via name/category ILIKE."""
    from jim.db import connect

    needle = "%" + "%".join(exercise.strip().upper().split()) + "%"
    since = date.today() - timedelta(days=days)
    with connect() as conn:
        rows = conn.execute(
            "SELECT day, category, exercise_name, reps, weight_kg FROM exercise_sets"
            " WHERE day >= %s AND (exercise_name ILIKE %s OR category ILIKE %s)"
            " ORDER BY day DESC",
            (since, needle, needle),
        ).fetchall()
    return summarize_exercise_history(rows)


def workout_history(days: int = 14) -> str:
    """Recent workouts + adherence (DB-backed tool for the coach)."""
    from jim.db import connect

    since = date.today() - timedelta(days=days)
    with connect() as conn:
        acts = conn.execute(
            "SELECT day, type, duration_min FROM garmin_activities"
            " WHERE day >= %s ORDER BY day DESC",
            (since,),
        ).fetchall()
        outcomes = conn.execute(
            "SELECT s.for_date, s.plan->>'title' AS title, o.adhered, o.notes"
            " FROM suggestions s JOIN outcomes o ON o.suggestion_id = s.id"
            " WHERE s.for_date >= %s ORDER BY s.for_date DESC",
            (since,),
        ).fetchall()
    lines = [
        f"{a['day']}: {a['type']} ({a['duration_min']:.0f} min)" for a in acts
    ] or ["(no activities recorded)"]
    if outcomes:
        lines.append("Plan adherence:")
        lines += [
            f"{o['for_date']}: {o['title']} — {'done' if o['adhered'] else 'missed'}"
            f" ({o['notes']})"
            for o in outcomes
        ]
    return "\n".join(lines)
