"""Schema coercions for LLM output. The model routinely sends explicit `null`
for a field that has a non-optional default instead of omitting the key —
without coercion that's a hard validation failure, and coach._parse_draft
drops the WHOLE day on any one field's error. Caught testing against a real
account, where several otherwise-correct days silently vanished from a
7-day plan."""

from datetime import date

from jim.schemas import ExerciseStep, StructuredSession


def test_null_sets_falls_back_to_the_default_instead_of_rejecting():
    step = ExerciseStep(exercise="Plank", sets=None, duration_sec=40)
    assert step.sets == 1


def test_explicit_sets_value_is_unaffected():
    step = ExerciseStep(exercise="Goblet squat", sets=3, reps=12)
    assert step.sets == 3


def test_null_est_duration_min_falls_back_to_zero():
    s = StructuredSession(for_date=date(2026, 8, 12), kind="rest", title="Rest",
                           est_duration_min=None)
    assert s.est_duration_min == 0.0


def test_explicit_est_duration_min_is_unaffected():
    s = StructuredSession(for_date=date(2026, 8, 12), kind="strength", title="Lift",
                           est_duration_min=45.0)
    assert s.est_duration_min == 45.0
