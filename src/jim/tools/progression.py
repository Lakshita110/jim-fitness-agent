"""Next-week load suggestions and workout-change flags, from real logged sets.

Pure functions over plain rows (no DB, no Garmin) so every rule unit-tests.
`mcp_server.get_week_overview` feeds them DB rows and hands the result to
Claude, which decides — against the athlete's constraints — what to actually
propose. Nothing here writes anything.

The progression rule is plain double progression, chosen because it's
transparent enough for the athlete to check by eye:
- all working reps at the top of the target range -> add the next load step
  and reset reps to the bottom of the range
- inside the range -> same load, aim for more reps
- below the range -> repeat the load; below it two sessions running at the
  same load -> step back down one increment
Two guards sit on top: a high-load / low-recovery week turns every
"increase" into "hold", and the watch's rep counts are treated as noisy
(median of working sets, not the worst set; missing counts are skipped).

Loads are stepped in the athlete's own unit: Garmin stores kg, but weights
entered in pounds come back as 4.56 / 5.69 / 9.06 kg (10 / 12.5 / 20 lb), and
"+2 kg" on those would be a dumbbell they don't own."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from jim.tools.history import classify_muscle_group

LB_PER_KG = 2.20462
DEFAULT_REP_RANGE = (8, 12)
_UNIT_TOLERANCE_KG = 0.05
# Real isometric holds — only these progress by time. Anything else with a
# duration and no reps is the watch not counting, not a hold.
_HOLD_WORDS = ("PLANK", "WALL_SQUAT", "WALL_SIT", "HOLD", "ISOMETRIC", "DEAD_HANG", "HOLLOW")
# Warm-up / mobility drills: logged, but loading them isn't a thing.
_DRILL_WORDS = ("STRETCH", "CIRCLES", "SWINGS", "CHILDS_POSE", "CARDIO", "WARM_UP", "FOAM_ROLL")


def is_drill(name: str) -> bool:
    return any(w in name.upper() for w in _DRILL_WORDS)


def _is_hold(name: str) -> bool:
    return any(w in name.upper() for w in _HOLD_WORDS)


def _range(lo: int, hi: int) -> str:
    return str(hi) if lo == hi else f"{lo}-{hi}"


# --- units ----------------------------------------------------------------------


def _off_grid(value: float, step: float) -> float:
    """Distance from the nearest multiple of `step`."""
    return abs(value - round(value / step) * step)


def detect_unit(weights_kg: list[float]) -> str:
    """'lb' if these weights sit on a half-pound grid but not a half-kilo one.

    Majority vote across an exercise's sets, ties to kg — a plate-loaded 20 kg
    is also 44.09 lb, so only an off-kilo-grid weight is evidence of pounds."""
    lb_votes = kg_votes = 0
    for w in weights_kg:
        if w <= 0:
            continue
        if _off_grid(w, 0.5) <= _UNIT_TOLERANCE_KG:
            kg_votes += 1
        elif _off_grid(w * LB_PER_KG, 0.5) / LB_PER_KG <= _UNIT_TOLERANCE_KG:
            lb_votes += 1
    return "lb" if lb_votes > kg_votes else "kg"


def to_unit(weight_kg: float, unit: str) -> float:
    if unit == "lb":
        return round(weight_kg * LB_PER_KG * 2) / 2  # nearest half pound
    return round(weight_kg * 2) / 2  # nearest half kilo


def from_unit(weight: float, unit: str) -> float:
    return round(weight / LB_PER_KG, 2) if unit == "lb" else weight


def load_step(weight: float, unit: str) -> float:
    """The next common load increment, in the exercise's own unit: small
    jumps for light dumbbells, plate-sized jumps once loads get heavier."""
    if unit == "lb":
        return 2.5 if weight < 25 else 5.0
    if weight < 10:
        return 1.0
    return 2.5 if weight < 40 else 5.0


# --- sessions --------------------------------------------------------------------


def summarize_sessions(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """exercise -> sessions (oldest first). Each session: day, top load (kg),
    reps of the working sets at that load, set count, and total duration for
    timed sets. Warm-up sets are anything lighter than the day's top load."""
    by_ex: dict[str, dict[date, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        name = r.get("exercise_name") or r.get("category")
        if name:
            by_ex[name][r["day"]].append(r)

    out: dict[str, list[dict[str, Any]]] = {}
    for name, days in by_ex.items():
        sessions = []
        for day in sorted(days):
            sets = days[day]
            weights = [float(s["weight_kg"]) for s in sets if s.get("weight_kg")]
            top = max(weights) if weights else None
            working = [s for s in sets if top is None or float(s.get("weight_kg") or 0) == top]
            reps = [int(s["reps"]) for s in working if s.get("reps")]
            durations = [float(s["duration_sec"]) for s in sets if s.get("duration_sec")]
            sessions.append({
                "day": day,
                "top_kg": top,
                "reps": reps,
                "sets": len(sets),
                "duration_sec": round(median(durations)) if durations and not reps else None,
            })
        out[name] = sessions
    return out


def _median_reps(session: dict[str, Any]) -> float | None:
    return median(session["reps"]) if session["reps"] else None


# --- suggestion -----------------------------------------------------------------


def suggest_next(
    exercise: str,
    sessions: list[dict[str, Any]],
    rep_range: tuple[int, int] | None,
    hold_increases: str | None = None,
    planned_hold: bool = False,
) -> dict[str, Any]:
    """One exercise's recommendation for next week. `sessions` oldest first.
    `hold_increases` is a reason string when this week's load/recovery says
    not to add weight anywhere (increases become holds, deloads still apply)."""
    lo, hi = rep_range or DEFAULT_REP_RANGE
    last = sessions[-1]
    prev = sessions[-2] if len(sessions) > 1 else None
    # Unit from the latest loaded session: an old kg-entered set mustn't
    # outvote how the athlete loads it now. History renders per session.
    loaded = [s["top_kg"] for s in sessions if s["top_kg"]]
    unit = detect_unit(loaded[-1:])
    rng = _range(lo, hi)
    def _fmt_load(kg: float | None) -> str:
        if not kg:
            return "bodyweight"
        u = detect_unit([kg])
        return f"{to_unit(kg, u):g} {u}"

    base: dict[str, Any] = {
        "exercise": exercise,
        "group": classify_muscle_group(exercise),
        "unit": unit,
        "target_reps": rng + ("" if rep_range else " (default — no planned reps found)"),
        "history": [
            {
                "date": s["day"].isoformat(),
                "load": _fmt_load(s["top_kg"]),
                "reps": s["reps"] or None,
                **({"hold_sec": s["duration_sec"]} if s["duration_sec"] else {}),
            }
            for s in sessions[-4:]
        ],
    }

    def result(action: str, reason: str, load: float | None = None,
               reps: str | None = None, **extra: Any) -> dict[str, Any]:
        if action == "increase" and hold_increases:
            action, reason = "hold", f"{reason} — but {hold_increases}, so hold this week"
            load = to_unit(last["top_kg"], unit) if last["top_kg"] else None
            reps = reps and rng
        if base["group"] == "legs" and action in ("increase", "harder_variation"):
            extra.setdefault(
                "caution", "lower body — check knee/ankle constraints before increasing")
        return {
            **base,
            "action": action,
            "next_load": f"{load:g} {unit}" if load is not None else None,
            **({"next_load_kg": from_unit(load, unit)} if load is not None else {}),
            "next_reps": reps,
            "reason": reason,
            **extra,
        }

    # Timed holds (planks, wall sits by time): progress the hold. A duration
    # on anything else just means the watch didn't count reps.
    if not last["reps"] and last["duration_sec"]:
        now = last["duration_sec"]
        if planned_hold or (_is_hold(exercise) and now >= 10):
            target = now + max(5, round(now * 0.1))
            return result("add_time", f"held {now}s last session", reps=f"{target}s")

    reps_now = _median_reps(last)
    if reps_now is None:
        return result(
            "hold", "the watch didn't count reps last session — ask how it went",
            load=to_unit(last["top_kg"], unit) if last["top_kg"] else None,
        )

    # Bodyweight: nothing to add but reps (or a harder variation).
    if not last["top_kg"]:
        if reps_now < lo:
            return result("hold",
                          f"watch counted a median {reps_now:g} reps, under {rng} — likely"
                          " miscounted; ask how many they actually did", reps=rng)
        if reps_now >= hi:
            return result("harder_variation",
                          f"median {reps_now:g} reps is at the top of {rng} with bodyweight"
                          " — add load or a harder variation, if constraints allow",
                          reps=rng)
        return result("add_reps", f"median {reps_now:g} reps", reps=f"{min(int(reps_now) + 2, hi)}")

    load_now = to_unit(last["top_kg"], unit)
    step = load_step(load_now, unit)
    if reps_now >= hi:
        return result("increase",
                      f"median {reps_now:g} reps at {load_now:g} {unit} hit the top of {rng}",
                      load=load_now + step, reps=rng)
    if reps_now < lo:
        prev_reps = _median_reps(prev) if prev else None
        same_load = prev is not None and prev["top_kg"] == last["top_kg"]
        if same_load and prev_reps is not None and prev_reps < lo:
            return result("deload",
                          f"below {lo} reps at {load_now:g} {unit} two sessions running",
                          load=max(load_now - step, step), reps=rng)
        return result("hold", f"median {reps_now:g} reps is under {lo} — repeat the load",
                      load=load_now, reps=rng)
    return result("add_reps", f"median {reps_now:g} reps at {load_now:g} {unit}, inside {rng}",
                  load=load_now, reps=_range(min(int(reps_now) + 1, hi), hi))


def is_stalled(sessions: list[dict[str, Any]]) -> bool:
    """Three+ sessions with no gain in load and no gain in reps at that load."""
    if len(sessions) < 3:
        return False
    a, b, c = sessions[-3:]
    if not (a["top_kg"] and b["top_kg"] and c["top_kg"]):
        return False
    if c["top_kg"] > a["top_kg"]:
        return False
    ra, rc = _median_reps(a), _median_reps(c)
    return ra is not None and rc is not None and rc <= ra and c["top_kg"] >= a["top_kg"]


def progression_report(
    rows: list[dict[str, Any]],
    planned: dict[str, Any],
    as_of: date,
    hold_increases: str | None,
    recent_days: int = 21,
) -> list[dict[str, Any]]:
    """Suggestions for every exercise trained in the last `recent_days`,
    most recently trained first. `planned` maps a Garmin exercise name
    (e.g. GOBLET_SQUAT) to {"reps": int|None, "duration_sec": int|None} from
    the athlete's plans (a bare int is read as reps). Warm-up/mobility drills
    are skipped (callers count them with `is_drill`)."""
    out = []
    for name, sessions in summarize_sessions(rows).items():
        if sessions[-1]["day"] < as_of - timedelta(days=recent_days) or is_drill(name):
            continue
        p = planned.get(name)
        if not isinstance(p, dict):
            p = {"reps": p, "duration_sec": None}
        target = p.get("reps")
        rep_range = (max(target - 4, 1), target) if target else None
        entry = suggest_next(name, sessions, rep_range, hold_increases,
                             planned_hold=bool(p.get("duration_sec")) and not target)
        if is_stalled(sessions):
            entry["stalled"] = True
        out.append(entry)
    out.sort(key=lambda e: e["history"][-1]["date"], reverse=True)
    return out


def workout_changes(
    progression: list[dict[str, Any]],
    adherence: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    readiness: dict[str, Any],
    as_of: date,
) -> list[str]:
    """Plain-language flags for next week's plan. Heuristics for Claude to
    weigh, not decisions — each names the evidence behind it."""
    changes: list[str] = []

    stalled = [p["exercise"] for p in progression if p.get("stalled")]
    if stalled:
        changes.append(
            f"Stalled for 3+ sessions (no load or rep gain): {', '.join(stalled)} — swap to"
            " a close variation or take a lighter week on it."
        )

    missed = [a for a in adherence if a["status"] in ("missed", "did_something_else")]
    if len(missed) >= 2:
        kinds = ", ".join(sorted({a["kind"] for a in missed}))
        changes.append(
            f"{len(missed)} planned sessions last week weren't done as planned ({kinds}) —"
            " consider fewer or shorter sessions, or moving them to days that worked."
        )

    acwr = readiness.get("acwr")
    if readiness.get("status") in ("ease", "rest") or (acwr is not None and acwr > 1.3):
        changes.append(
            f"Load is high relative to the last month (ACWR {acwr}) — plan about 20% less"
            " volume next week; hold weights rather than add."
        )
    elif acwr is not None and acwr < 0.8:
        changes.append(
            f"Load is well under the recent average (ACWR {acwr}) — there's room for an"
            " extra session or a few more sets, if recovery and constraints allow."
        )

    recent = {classify_muscle_group(r.get("exercise_name") or r.get("category") or "")
              for r in rows if r["day"] >= as_of - timedelta(days=14)}
    earlier = {classify_muscle_group(r.get("exercise_name") or r.get("category") or "")
               for r in rows if r["day"] < as_of - timedelta(days=14)}
    for group in ("legs", "push", "pull", "core"):
        if group in earlier and group not in recent:
            changes.append(f"No {group} work logged in the last 14 days (there was before).")
    return changes
