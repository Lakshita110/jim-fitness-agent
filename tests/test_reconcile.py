from datetime import UTC, date, datetime, timedelta

from jim.jobs.reconcile import adhered, needs_replan
from jim.schemas import ActivitySummary, CheckIn, ExerciseStep, StructuredSession

FOR_DATE = date(2026, 7, 6)


def plan(kind="strength", minutes=45.0) -> StructuredSession:
    return StructuredSession(
        for_date=FOR_DATE,
        kind=kind,
        title="t",
        steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8)],
        est_duration_min=minutes,
    )


def activity(type_="strength_training", minutes=45.0) -> ActivitySummary:
    return ActivitySummary(activity_id="a1", type=type_, duration_min=minutes)


def test_matching_activity_adheres():
    ok, _ = adhered(plan(), [activity()])
    assert ok


def test_wrong_kind_does_not_adhere():
    ok, notes = adhered(plan(), [activity(type_="running")])
    assert not ok
    assert "no strength activity" in notes


def test_duration_way_off_does_not_adhere():
    ok, notes = adhered(plan(minutes=60), [activity(minutes=10)])
    assert not ok
    assert "duration off" in notes


def test_rest_day_respected_and_violated():
    ok, _ = adhered(plan(kind="rest"), [])
    assert ok
    ok, _ = adhered(plan(kind="rest"), [activity()])
    assert not ok


def test_multiple_activities_summed():
    ok, _ = adhered(plan(minutes=60), [activity(minutes=30), activity(minutes=25)])
    assert ok


# --- morning re-plan decision -------------------------------------------------

NIGHTLY_RUN = datetime(2026, 7, 5, 21, 5, tzinfo=UTC)  # last night's proposal


def checkin(minutes_after_nightly: int | None, **fields) -> CheckIn:
    edited = (
        NIGHTLY_RUN + timedelta(minutes=minutes_after_nightly)
        if minutes_after_nightly is not None
        else None
    )
    return CheckIn(for_date=FOR_DATE, edited_ts=edited, **fields)


def test_empty_checkin_never_replans():
    assert not needs_replan(checkin(600), NIGHTLY_RUN)  # edited late but says nothing


def test_morning_checkin_after_nightly_replans():
    ci = checkin(600, focus="pt only", location="home")  # ~7am, after 9pm run
    assert needs_replan(ci, NIGHTLY_RUN)


def test_evening_checkin_already_seen_does_not_replan():
    ci = checkin(-120, focus="upper")  # written at 7pm, before the nightly run
    assert not needs_replan(ci, NIGHTLY_RUN)


def test_checkin_with_no_prior_proposal_replans():
    ci = checkin(600, note="knee flaring")
    assert needs_replan(ci, None)


def test_checkin_without_edit_timestamp_is_conservative():
    ci = checkin(None, focus="upper")  # can't tell when it was written
    assert not needs_replan(ci, NIGHTLY_RUN)  # keep last night's plan
    assert needs_replan(ci, None)  # but plan if there is no plan at all
