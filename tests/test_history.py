from datetime import date, timedelta

from jim.tools.history import (
    activity_groups,
    classify_muscle_group,
    compute_features,
    compute_readiness,
    days_since_legs,
    muscle_group_balance,
    pain_trend,
    recent_pain_notes,
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


def test_classify_muscle_group_ignores_separators():
    """Garmin logs sets as DUMBBELL_STEP_UP; a hyphenated needle missed them."""
    assert classify_muscle_group("DUMBBELL_STEP_UP") == "legs"
    assert classify_muscle_group("ROMANIAN_DEADLIFT") == "legs"
    assert classify_muscle_group("BANDED_PULL_UPS") == "pull"
    assert classify_muscle_group("LAT_PULLDOWN") == "pull"


def test_needles_match_on_word_boundaries_not_mid_word():
    """"ab" matched the middle of c-AB-le, so every cable movement scored as abs."""
    assert classify_muscle_group("CABLE_CROSSOVER") == "push"
    assert classify_muscle_group("SEATED_CABLE_ROW") == "pull"
    assert classify_muscle_group("CABLE_CURL") == "pull"
    # ...while genuine ab work still lands in core
    assert classify_muscle_group("AB_WHEEL") == "core"
    assert classify_muscle_group("ABS") == "core"


def test_classify_covers_the_real_logged_movements():
    """Names taken verbatim from the athlete's Garmin sets — these fell through
    to "other" and vanished from the balance."""
    for name, group in [
        ("DUMBBELL_FLYE", "push"),
        ("SHRUG", "pull"),
        ("SIT_UP", "core"),
        ("TRICEPS_EXTENSION", "push"),
        ("SHOULDER_PRESS", "push"),
        ("LEG_EXTENSIONS", "legs"),
    ]:
        assert classify_muscle_group(name) == group, name


def test_activity_groups_reads_muscles_from_the_sets():
    """A strength session's type is just "strength_training" — without reading
    its sets, 20 leg sessions look like "never trained legs"."""
    act = {"day": AS_OF, "type": "strength_training", "duration_min": 60,
           "exercises": ["BARBELL_SQUAT", "ROMANIAN_DEADLIFT", "BENCH_PRESS", "MYSTERY"]}
    groups = activity_groups(act)
    assert groups == {"legs": 2 / 3, "push": 1 / 3}   # unclassifiable set dropped
    assert abs(sum(groups.values()) - 1.0) < 1e-9


def test_activity_groups_falls_back_to_type_without_sets():
    assert activity_groups({"type": "cycling"}) == {"conditioning": 1.0}
    assert activity_groups({"type": "mobility"}) == {"mobility": 1.0}
    assert activity_groups({"type": "yoga"}) == {"mobility": 1.0}
    # a strength session with no logged sets is honest about being unsplit
    assert activity_groups({"type": "strength_training"}) == {"strength": 1.0}


def test_indoor_rowing_is_conditioning_not_back_work():
    """Substring matching scored the erg as "pull" because it contains "row"."""
    assert activity_groups({"type": "indoor_rowing"}) == {"conditioning": 1.0}


def test_days_since_legs_sees_legs_inside_a_strength_session():
    acts = [
        {"day": date(2026, 7, 5), "type": "strength_training", "duration_min": 60,
         "exercises": ["LEG_EXTENSIONS", "LAT_PULLDOWN"]},
        {"day": AS_OF, "type": "cycling", "duration_min": 45, "exercises": []},
    ]
    assert days_since_legs(acts, AS_OF) == 1


def test_muscle_group_balance_splits_a_mixed_session():
    acts = [{"day": AS_OF, "type": "strength_training", "duration_min": 60,
             "exercises": ["SQUAT", "SQUAT", "BENCH_PRESS", "BARBELL_ROW"]}]
    balance = muscle_group_balance(acts, AS_OF)
    assert balance == {"legs": 0.5, "push": 0.25, "pull": 0.25}


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


def test_recent_pain_notes_are_newest_first_with_context():
    logs = [
        {"day": date(2026, 7, 4), "pain_level": 3, "pain_location": "right",
         "pain_notes": "might've been triggered by driving"},
        {"day": date(2026, 7, 6), "pain_level": 5, "pain_location": "wrists",
         "pain_notes": "wrists still poor"},
        {"day": date(2026, 7, 5), "pain_level": None, "pain_location": "",
         "pain_notes": "   "},                                   # blank note skipped
        {"day": date(2026, 7, 3), "pain_level": 2, "pain_location": "left",
         "pain_notes": ""},                                      # no note skipped
    ]
    notes = recent_pain_notes(logs)
    assert notes == [
        "2026-07-06 (wrists, 5/10): wrists still poor",
        "2026-07-04 (right, 3/10): might've been triggered by driving",
    ]


def test_recent_pain_notes_caps_the_list():
    logs = [{"day": date(2026, 7, i), "pain_level": 1, "pain_location": "knee",
             "pain_notes": f"note {i}"} for i in range(1, 12)]
    notes = recent_pain_notes(logs)
    assert len(notes) == 6                 # MAX_PAIN_NOTES
    assert notes[0].startswith("2026-07-11")   # newest first


def test_recent_pain_notes_tolerates_missing_fields():
    assert recent_pain_notes([{"day": date(2026, 7, 6)}]) == []
    assert recent_pain_notes([]) == []


def test_compute_features_assembles_everything():
    logs = [{"day": date(2026, 7, d), "pain_level": d % 3} for d in range(1, 7)]
    daily = [{"day": AS_OF, "readiness": 60}, {"day": date(2026, 7, 5), "readiness": 40}]
    f = compute_features(AS_OF, 28, ACTIVITIES, logs, daily)
    assert f.weekly_volume_min == 115
    assert f.days_since_legs == 0
    assert f.avg_readiness == 50


def test_avg_readiness_falls_back_to_body_battery():
    """This athlete's watch never reports Training Readiness — readiness is null
    on every row — so a readiness-only average was permanently None."""
    daily = [
        {"day": AS_OF, "readiness": None, "body_battery": 70},
        {"day": date(2026, 7, 5), "readiness": None, "body_battery": 30},
    ]
    f = compute_features(AS_OF, 28, [], [], daily)
    assert f.avg_readiness == 50


def test_avg_readiness_prefers_readiness_when_present():
    daily = [{"day": AS_OF, "readiness": 80, "body_battery": 20}]
    assert compute_features(AS_OF, 28, [], [], daily).avg_readiness == 80


# --- load & readiness verdict ---------------------------------------------


def _load_act(day: date, load: float) -> dict:
    return {"day": day, "duration_min": 40, "training_load": load}


def test_readiness_uses_training_load_when_present():
    # Steady base of 100/week for 4 weeks, so chronic avg-week = 100.
    acts = [_load_act(AS_OF - timedelta(days=d), 100 / 7) for d in range(28)]
    r = compute_readiness(AS_OF, acts, [{"day": AS_OF, "readiness": 70}])
    assert r.basis == "load"
    assert r.acwr is not None and 0.9 <= r.acwr <= 1.1
    assert r.status == "steady"


def test_readiness_flags_load_spike_as_ease():
    # Light chronic base, heavy last week -> ACWR well above 1.5.
    acts = [_load_act(AS_OF - timedelta(days=d), 5) for d in range(8, 28)]
    acts += [_load_act(AS_OF - timedelta(days=d), 60) for d in range(0, 7)]
    r = compute_readiness(AS_OF, acts, [{"day": AS_OF, "readiness": 65}])
    assert r.acwr > 1.5
    assert r.status == "ease"


def test_readiness_poor_recovery_overrides_to_rest():
    acts = [_load_act(AS_OF - timedelta(days=d), 100 / 7) for d in range(28)]
    r = compute_readiness(AS_OF, acts, [{"day": AS_OF, "readiness": 20}])
    assert r.status == "ease"  # low readiness pulls steady down
    acts_spike = [_load_act(AS_OF - timedelta(days=d), 5) for d in range(8, 28)]
    acts_spike += [_load_act(AS_OF - timedelta(days=d), 60) for d in range(0, 7)]
    r2 = compute_readiness(AS_OF, acts_spike, [{"day": AS_OF, "body_battery": 25}])
    assert r2.status == "rest"  # ease + very low recovery -> rest


def test_readiness_falls_back_to_minutes_without_load():
    acts = [{"day": AS_OF - timedelta(days=d), "duration_min": 30, "training_load": None}
            for d in range(28)]
    r = compute_readiness(AS_OF, acts, [{"day": AS_OF, "readiness": 60}])
    assert r.basis == "minutes"
    assert r.acwr is not None


def test_readiness_no_data_is_steady():
    r = compute_readiness(AS_OF, [], [])
    assert r.status == "steady"
    assert r.acwr is None
    assert r.basis == "none"
    assert "not enough" in r.detail
