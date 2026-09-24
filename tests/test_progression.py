"""tools/progression.py: next-week load suggestions from logged sets.

Fixtures use the shapes seen in a real account's data: loads entered in
pounds (Garmin stores 4.56 / 5.69 / 9.06 / 18.12 kg for 10 / 12.5 / 20 /
40 lb), noisy watch rep counts, sets with no count at all, and bodyweight
and timed PT moves."""

from datetime import date, timedelta

import pytest

from jim.tools import progression as p

D = date(2026, 9, 24)


def _sets(day, name, reps, weight=None, duration=None):
    return [{"day": day, "exercise_name": name, "category": None, "reps": r,
             "weight_kg": weight, "duration_sec": duration} for r in reps]


# --- units ---------------------------------------------------------------------


@pytest.mark.parametrize("kg,unit", [
    (4.56, "lb"), (5.69, "lb"), (9.06, "lb"), (18.12, "lb"),
    (47.0, "kg"), (62.0, "kg"), (10.0, "kg"), (2.5, "kg"), (20.0, "kg"),
])
def test_unit_detection_matches_how_the_athlete_actually_loads(kg, unit):
    assert p.detect_unit([kg]) == unit


def test_unit_conversions_round_to_real_plate_sizes():
    assert p.to_unit(5.69, "lb") == 12.5
    assert p.to_unit(18.12, "lb") == 40.0
    assert p.to_unit(47.0, "kg") == 47.0
    assert p.from_unit(15.0, "lb") == pytest.approx(6.8, abs=0.01)


def test_load_steps_scale_with_the_weight():
    assert p.load_step(12.5, "lb") == 2.5
    assert p.load_step(40, "lb") == 5.0
    assert p.load_step(6, "kg") == 1.0
    assert p.load_step(20, "kg") == 2.5
    assert p.load_step(60, "kg") == 5.0


# --- sessions --------------------------------------------------------------------


def test_warmup_sets_are_excluded_from_working_reps():
    rows = (_sets(D, "LEG_PRESS", [15], weight=20.0)
            + _sets(D, "LEG_PRESS", [10, 9], weight=47.0))
    (session,) = p.summarize_sessions(rows)["LEG_PRESS"]
    assert session["top_kg"] == 47.0
    assert session["reps"] == [10, 9]
    assert session["sets"] == 3


def test_uncounted_reps_are_ignored_not_zero():
    rows = _sets(D, "STEP_UP", [None, 10, None])
    (session,) = p.summarize_sessions(rows)["STEP_UP"]
    assert session["reps"] == [10]


# --- the double-progression rule ---------------------------------------------------------


def _suggest(sessions_spec, rep_range=(8, 12), hold=None, name="SEATED_CABLE_ROW"):
    rows = []
    for i, (weight, reps) in enumerate(sessions_spec):
        rows += _sets(D - timedelta(days=7 * (len(sessions_spec) - 1 - i)), name, reps, weight)
    sessions = p.summarize_sessions(rows)[name]
    return p.suggest_next(name, sessions, rep_range, hold)


def test_top_of_range_increases_by_one_step_in_the_athletes_unit():
    s = _suggest([(9.06, [12, 12, 13])])  # 20 lb
    assert s["action"] == "increase"
    assert s["unit"] == "lb"
    assert s["next_load"] == "22.5 lb"
    assert s["next_reps"] == "8-12"
    assert s["next_load_kg"] == pytest.approx(10.21, abs=0.01)


def test_inside_range_adds_reps_at_the_same_load():
    s = _suggest([(18.12, [10, 9, 10])])
    assert s["action"] == "add_reps"
    assert s["next_load"] == "40 lb"
    assert s["next_reps"] == "11-12"


def test_below_range_once_holds():
    s = _suggest([(18.12, [10, 10, 10]), (22.68, [7, 6, 7])])
    assert s["action"] == "hold"
    assert s["next_load"] == "50 lb"


def test_below_range_twice_at_the_same_load_deloads():
    s = _suggest([(22.68, [7, 6, 7]), (22.68, [6, 6, 7])])
    assert s["action"] == "deload"
    assert s["next_load"] == "45 lb"


def test_noisy_single_low_set_does_not_block_progress():
    """Median, not min: one miscounted set (the watch said 3) shouldn't
    read as a failed session."""
    s = _suggest([(47.0, [12, 3, 12])], name="LEG_PRESS")
    assert s["action"] == "increase"
    assert s["next_load"] == "52 kg"


def test_high_load_week_turns_increases_into_holds_but_not_deloads():
    reason = "load is high this week (ACWR 1.6)"
    s = _suggest([(9.06, [12, 12, 12])], hold=reason)
    assert s["action"] == "hold"
    assert s["next_load"] == "20 lb"
    assert reason in s["reason"]
    s = _suggest([(22.68, [6, 6]), (22.68, [6, 6])], hold=reason)
    assert s["action"] == "deload"


def test_planned_reps_set_the_target_range():
    s = _suggest([(9.06, [12, 12])], rep_range=(11, 15))
    assert s["action"] == "add_reps"
    assert s["target_reps"] == "11-15"


def test_missing_rep_counts_hold_and_say_to_ask():
    s = _suggest([(9.06, [None, None])])
    assert s["action"] == "hold"
    assert "ask" in s["reason"]


def test_bodyweight_moves_progress_by_reps_then_variation():
    s = _suggest([(None, [10, 10])], name="STEP_UP")
    assert s["action"] == "add_reps" and s["next_load"] is None
    s = _suggest([(None, [12, 13])], name="STEP_UP")
    assert s["action"] == "harder_variation"


def test_timed_holds_progress_the_hold():
    rows = _sets(D, "PLANK", [None, None], duration=40)
    s = p.suggest_next("PLANK", p.summarize_sessions(rows)["PLANK"], None)
    assert s["action"] == "add_time"
    assert s["next_reps"] == "45s"


def test_stall_is_three_sessions_without_load_or_rep_gain():
    rows = []
    for i, reps in enumerate(([10, 10], [10, 9], [9, 9])):
        rows += _sets(D - timedelta(days=7 * (2 - i)), "FACE_PULL", reps, 25.0)
    assert p.is_stalled(p.summarize_sessions(rows)["FACE_PULL"])
    rows += _sets(D + timedelta(days=7), "FACE_PULL", [10, 10], 27.5)
    assert not p.is_stalled(p.summarize_sessions(rows)["FACE_PULL"])


# --- report + changes -------------------------------------------------------------------


def test_report_only_covers_recently_trained_exercises():
    rows = (_sets(D - timedelta(days=3), "ROPE_PRESSDOWN", [12, 12], 9.06)
            + _sets(D - timedelta(days=40), "CABLE_BICEPS_CURL", [10], 2.5))
    report = p.progression_report(rows, {}, D, None)
    assert [r["exercise"] for r in report] == ["ROPE_PRESSDOWN"]


def test_report_uses_planned_reps_when_known():
    rows = _sets(D, "ROPE_PRESSDOWN", [12, 12], 9.06)
    (entry,) = p.progression_report(rows, {"ROPE_PRESSDOWN": 15}, D, None)
    assert entry["target_reps"] == "11-15"
    assert entry["action"] == "add_reps"


def test_changes_flag_misses_load_and_quiet_muscle_groups():
    rows = (_sets(D - timedelta(days=20), "SEATED_CABLE_ROW", [10], 18.12)
            + _sets(D - timedelta(days=2), "LEG_PRESS", [10], 47.0))
    adherence = [{"status": "missed", "kind": "strength"},
                 {"status": "did_something_else", "kind": "running"},
                 {"status": "done", "kind": "strength"}]
    changes = p.workout_changes([], adherence, rows, {"status": "ease", "acwr": 1.6}, D)
    text = " | ".join(changes)
    assert "2 planned sessions" in text
    assert "ACWR 1.6" in text
    assert "No pull work" in text
    assert "legs" not in text  # trained 2 days ago


def test_low_load_suggests_room_to_add():
    changes = p.workout_changes([], [], [], {"status": "push", "acwr": 0.6}, D)
    assert any("room for an extra session" in c for c in changes)


# --- quality fixes found on real data ----------------------------------------------------


def test_unit_follows_the_latest_session_not_old_history():
    """An old 2.5 kg session outvoted a current 12.5 lb one -> '5.5 kg'."""
    s = _suggest([(2.5, [10]), (2.5, [10]), (5.69, [12, 12])], name="BENCH_PRESS")
    assert s["unit"] == "lb" and s["next_load"] == "15 lb"
    assert s["history"][0]["load"] == "2.5 kg"


def test_short_durations_on_non_holds_are_not_holds():
    rows = _sets(D, "SHOULDER_CIRCLES", [None], duration=2) + _sets(
        D, "FARMERS_CARRY", [None], weight=9.06, duration=6)
    sessions = p.summarize_sessions(rows)
    for name in ("SHOULDER_CIRCLES", "FARMERS_CARRY"):
        s = p.suggest_next(name, sessions[name], None)
        assert s["action"] == "hold" and "ask" in s["reason"]


def test_planned_hold_progresses_by_time():
    rows = _sets(D, "SIDE_BRIDGE", [None], duration=30)
    (e,) = p.progression_report(rows, {"SIDE_BRIDGE": {"reps": None, "duration_sec": 30}},
                                D, None)
    assert e["action"] == "add_time"


def test_warmup_drills_are_left_out():
    rows = _sets(D, "ARM_CIRCLES", [10]) + _sets(D, "ROPE_PRESSDOWN", [12], 9.06)
    assert [e["exercise"] for e in p.progression_report(rows, {}, D, None)] == ["ROPE_PRESSDOWN"]


def test_bodyweight_below_range_holds_as_a_likely_miscount():
    s = _suggest([(None, [5, 5, 6])], name="CLAM_SHELLS")
    assert s["action"] == "hold" and "miscounted" in s["reason"]


def test_lower_body_increases_carry_a_caution():
    s = _suggest([(47.0, [12, 12])], name="LEG_PRESS")
    assert s["action"] == "increase" and "knee/ankle" in s["caution"]


def test_single_rep_target_prints_as_one_number():
    s = _suggest([(9.06, [10, 10])], rep_range=(12, 12))
    assert s["target_reps"] == "12"
