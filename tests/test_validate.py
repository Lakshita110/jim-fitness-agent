from datetime import date

from jim.agent.validate import fallback_session, validate
from jim.schemas import ExerciseStep, HistoryFeatures, StructuredSession

FOR_DATE = date(2026, 7, 7)


def features(**overrides) -> HistoryFeatures:
    base = {"as_of": FOR_DATE, "window_days": 28, "weekly_volume_min": 200, "days_since_legs": 3}
    base.update(overrides)
    return HistoryFeatures(**base)


def session(**overrides) -> StructuredSession:
    base = StructuredSession(
        for_date=FOR_DATE,
        kind="strength",
        title="test",
        steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8)],
        est_duration_min=45,
    )
    return base.model_copy(update=overrides)


def test_sane_session_passes():
    assert validate(session(), features()).ok


def test_forbidden_exercise_rejected():
    bad = session(steps=[ExerciseStep(exercise="Box Jump", sets=3, reps=5)])
    result = validate(bad, features())
    assert not result.ok
    assert "forbidden" in result.violations[0]


def test_too_many_steps_rejected():
    bad = session(steps=[ExerciseStep(exercise="Bench press", sets=1, reps=5)] * 51)
    assert not validate(bad, features()).ok


def test_session_too_long_rejected():
    assert not validate(session(est_duration_min=180), features()).ok


def test_weekly_volume_cap():
    result = validate(session(est_duration_min=90), features(weekly_volume_min=550))
    assert not result.ok
    assert any("weekly volume" in v for v in result.violations)


def test_progression_too_steep():
    # 100 min existing, +60 min proposed = 60% jump >> 10% + 30min allowance
    result = validate(session(est_duration_min=90), features(weekly_volume_min=100))
    assert not result.ok
    assert any("progression" in v for v in result.violations)


def test_leg_day_spacing_enforced():
    legs = session(steps=[ExerciseStep(exercise="Goblet squat", sets=3, reps=8)])
    assert not validate(legs, features(days_since_legs=1)).ok
    assert validate(legs, features(days_since_legs=2)).ok


def test_fallback_is_always_valid():
    fb = fallback_session(session())
    assert fb.kind == "mobility"
    assert validate(fb, features(days_since_legs=0, weekly_volume_min=300)).ok
