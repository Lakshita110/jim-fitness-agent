from datetime import date, timedelta

from jim.agent.validate import fallback_session, validate, validate_plan, weekly_budget
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


# --- multi-day plans --------------------------------------------------------


def week(*specs) -> list[StructuredSession]:
    """specs: (day_offset, est_duration_min, exercise)"""
    return [
        session(
            for_date=date(2026, 7, 7) + timedelta(days=off),
            est_duration_min=mins,
            steps=[ExerciseStep(exercise=ex, sets=3, reps=8)],
        )
        for off, mins, ex in specs
    ]


def test_a_normal_week_is_buildable():
    """The bug the athlete hit: validate() judges ONE session against the
    trailing week, so per-day it demanded every single session fit inside last
    week's 10% headroom — on a 340-min base, any day over ~64 min. Days kept
    getting dropped and a full Mon-Fri plan was impossible to build."""
    f = features(weekly_volume_min=340, days_since_legs=None)
    plan = week(
        (0, 75, "PT protocol"), (1, 50, "Bench press"), (2, 60, "PT protocol"),
        (3, 50, "Lat pulldown"), (4, 40, "Hip mobility flow"),
    )
    assert sum(s.est_duration_min for s in plan) == 275
    assert validate_plan(plan, f) == {}          # 275 min fits the 404-min budget

    # ...while the old per-day rule rejected a 75-min day the week easily affords,
    # by testing it as "last week's 340 + this one day" against a weekly budget.
    assert not validate(plan[0], f).ok
    assert "progression too steep" in validate(plan[0], f).violations[0]


def test_planned_days_accumulate_against_the_weekly_budget():
    """The other half of the bug: per-day checks never accumulated, so a week of
    seven 90-minute sessions sailed through."""
    f = features(weekly_volume_min=340, days_since_legs=None)
    plan = week(*[(i, 90, "Bench press") for i in range(7)])
    violations = validate_plan(plan, f)
    assert violations                              # 630 min blows the 404 budget
    # the early days fit; the days that overspend it are the ones flagged
    assert "2026-07-07" not in violations
    assert "2026-07-13" in violations
    assert "budget" in violations["2026-07-13"][0]


def test_planned_leg_days_space_against_each_other():
    """Spacing only looked at history, so two planned leg days back-to-back
    both passed."""
    f = features(weekly_volume_min=340, days_since_legs=None)
    plan = week((0, 45, "Barbell squat"), (1, 45, "Romanian deadlift"))
    violations = validate_plan(plan, f)
    assert "2026-07-08" in violations
    assert "leg session only 1 day(s)" in violations["2026-07-08"][0]


def test_planned_leg_day_still_spaces_against_history():
    f = features(weekly_volume_min=340, days_since_legs=0)   # trained legs today
    plan = week((1, 45, "Barbell squat"))
    assert "2026-07-08" in validate_plan(plan, f)


def test_rest_days_do_not_spend_the_budget():
    f = features(weekly_volume_min=340, days_since_legs=None)
    plan = week((0, 60, "PT protocol")) + [
        session(for_date=date(2026, 7, 8), kind="rest", est_duration_min=0, steps=[])
    ]
    assert validate_plan(plan, f) == {}


def test_no_history_budgets_the_ceiling_not_thirty_minutes():
    """A 0-minute baseline would otherwise budget 0*1.1+30 = 30 min/week and
    reject every plan a new athlete could make."""
    f = features(weekly_volume_min=0, days_since_legs=None)
    assert weekly_budget(f) == 600
    assert validate_plan(week((0, 60, "PT protocol")), f) == {}


def test_plan_budget_never_exceeds_the_hard_ceiling():
    f = features(weekly_volume_min=5000, days_since_legs=None)
    assert weekly_budget(f) == 600


def test_per_session_rules_still_apply_inside_a_plan():
    f = features(weekly_volume_min=340, days_since_legs=None)
    plan = week((0, 45, "Box jump"), (1, 200, "Bench press"))
    violations = validate_plan(plan, f)
    assert "forbidden" in violations["2026-07-07"][0]
    assert any("exceeds max" in v for v in violations["2026-07-08"])
