"""Exercise-set parsing + progression summary — pure, fixture-based (shapes
recorded from the live Garmin API on this account)."""

from datetime import date

from jim.tools.garmin import parse_exercise_sets
from jim.tools.history import summarize_exercise_history

# Recorded shape from GET activity/{id}/exerciseSets (Full Body A, 2026-06-15).
RAW = {
    "exerciseSets": [
        {"setType": "ACTIVE", "repetitionCount": 12, "weight": None, "duration": 60.0,
         "exercises": [{"category": "SQUAT", "name": "GOBLET_SQUAT"}]},
        {"setType": "REST", "repetitionCount": None, "weight": None, "duration": 90.0,
         "exercises": []},
        {"setType": "ACTIVE", "repetitionCount": 8, "weight": 18000.0, "duration": 45.0,
         "exercises": [{"category": "BENCH_PRESS", "name": "DUMBBELL_BENCH_PRESS"}]},
        {"setType": "ACTIVE", "repetitionCount": 0, "weight": 18000.0, "duration": 40.0,
         "exercises": [{"category": "BENCH_PRESS", "name": "DUMBBELL_BENCH_PRESS"}]},
        {"setType": "ACTIVE", "repetitionCount": 17, "weight": None, "duration": 214.7,
         "exercises": [{"category": "TRICEPS_EXTENSION", "name": None}]},
    ]
}


def test_parse_exercise_sets_keeps_active_only_and_converts_grams():
    rows = parse_exercise_sets(RAW)
    assert len(rows) == 4  # REST dropped
    goblet = rows[0]
    assert goblet["category"] == "SQUAT" and goblet["exercise_name"] == "GOBLET_SQUAT"
    assert goblet["reps"] == 12 and goblet["weight_kg"] is None
    bench = rows[1]
    assert bench["weight_kg"] == 18.0  # grams -> kg
    missed = rows[2]
    assert missed["reps"] is None  # watch missed the count (0 -> unknown)
    unnamed = rows[3]
    assert unnamed["exercise_name"] is None and unnamed["category"] == "TRICEPS_EXTENSION"


def test_parse_handles_empty_payload():
    assert parse_exercise_sets({}) == []
    assert parse_exercise_sets({"exerciseSets": None}) == []


def rows_for_summary():
    return [
        {"day": date(2026, 7, 1), "category": "SQUAT", "exercise_name": "GOBLET_SQUAT",
         "reps": 12, "weight_kg": 16.0},
        {"day": date(2026, 7, 1), "category": "SQUAT", "exercise_name": "GOBLET_SQUAT",
         "reps": 10, "weight_kg": 16.0},
        {"day": date(2026, 7, 5), "category": "SQUAT", "exercise_name": "GOBLET_SQUAT",
         "reps": 12, "weight_kg": 18.0},
        {"day": date(2026, 7, 5), "category": "TRICEPS_EXTENSION", "exercise_name": None,
         "reps": 15, "weight_kg": None},
    ]


def test_summarize_groups_by_exercise_and_day_newest_first():
    text = summarize_exercise_history(rows_for_summary())
    goblet_line = next(line for line in text.splitlines() if "GOBLET_SQUAT" in line)
    # newest session first, rep range + max weight per day
    assert goblet_line.index("2026-07-05") < goblet_line.index("2026-07-01")
    assert "2026-07-05: 1x12 @ 18kg" in goblet_line
    assert "2026-07-01: 2x10-12 @ 16kg" in goblet_line
    # unnamed exercise falls back to category
    assert "TRICEPS_EXTENSION: 2026-07-05: 1x15" in text


def test_summarize_empty():
    assert "no logged sets" in summarize_exercise_history([])
