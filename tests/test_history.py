from datetime import date

from jim.tools.history import (
    classify_muscle_group,
    compute_features,
    days_since_legs,
    muscle_group_balance,
    pain_trend,
    weekly_volume_min,
)

AS_OF = date(2026, 7, 6)


def act(day: date, type_: str, minutes: float) -> dict:
    return {"day": day, "type": type_, "duration_min": minutes}


ACTIVITIES = [
    act(date(2026, 7, 6), "strength_training leg day squat", 45),
    act(date(2026, 7, 4), "running", 30),
    act(date(2026, 7, 1), "bench press session", 40),
    act(date(2026, 6, 20), "cycling", 60),  # outside the 7-day window
]


def test_classify_muscle_group():
    assert classify_muscle_group("Goblet Squat") == "legs"
    assert classify_muscle_group("Bench Press") == "push"
    assert classify_muscle_group("running") == "conditioning"
    assert classify_muscle_group("mystery move") == "other"


def test_weekly_volume_only_counts_last_7_days():
    assert weekly_volume_min(ACTIVITIES, AS_OF) == 45 + 30 + 40


def test_muscle_group_balance_fractions_sum_to_one():
    balance = muscle_group_balance(ACTIVITIES, AS_OF)
    assert set(balance) == {"legs", "conditioning", "push"}
    assert abs(sum(balance.values()) - 1.0) < 0.01
    assert balance["legs"] == round(45 / 115, 3)


def test_days_since_legs():
    assert days_since_legs(ACTIVITIES, AS_OF) == 0
    assert days_since_legs([act(date(2026, 7, 1), "running", 30)], AS_OF) is None


def test_pain_trend_positive_when_worsening():
    logs = [
        {"day": date(2026, 7, d), "pain_level": p}
        for d, p in [(1, 1), (2, 2), (3, 2), (4, 3), (5, 4)]
    ]
    assert pain_trend(logs) > 0.5


def test_pain_trend_handles_sparse_data():
    assert pain_trend([]) == 0.0
    assert pain_trend([{"day": AS_OF, "pain_level": 3}]) == 0.0
    assert pain_trend([{"day": AS_OF, "pain_level": None}] * 5) == 0.0


def test_compute_features_assembles_everything():
    logs = [{"day": date(2026, 7, d), "pain_level": d % 3} for d in range(1, 7)]
    daily = [{"day": AS_OF, "readiness": 60}, {"day": date(2026, 7, 5), "readiness": 40}]
    f = compute_features(AS_OF, 28, ACTIVITIES, logs, daily)
    assert f.weekly_volume_min == 115
    assert f.days_since_legs == 0
    assert f.avg_readiness == 50
