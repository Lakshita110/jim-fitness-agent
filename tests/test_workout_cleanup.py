"""cleanup_adapted_workouts: sweeps one-off Garmin workouts Claude created via
mcp_server.create_or_update_workout, by scanning the athlete's real workout
library for the ADAPTED_WORKOUT_PREFIX marker and checking each one's own
createdDate — there's no Jim-side record of having created them, so Garmin
is the only source of truth.

Previously this read get_scheduled_workouts (the calendar view) instead, but
Garmin drops a workout from that view once it's completed (moved to activity
history), which could orphan a completed one-off permanently if cleanup
didn't run before that happened. Scanning the library + createdDate instead
doesn't have that race — a workout's library entry and its createdDate don't
disappear on completion."""

from datetime import date

import jim.tools.garmin as garmin_mod
from jim.jobs.nightly import cleanup_adapted_workouts


def _workout(workout_id: str, name: str) -> dict:
    return {"workout_id": workout_id, "name": name, "sport": "strength_training"}


def _detail(created: str) -> dict:
    return {"createdDate": f"{created}T12:00:00.0"}


def test_cleanup_adapted_workouts_deletes_only_prefixed_past_items(monkeypatch):
    library = [
        _workout("aaa", "Jim · Legs — sore knee"),
        _workout("bbb", "Full Body A"),  # not ours
    ]
    details = {"aaa": _detail("2026-07-01")}
    monkeypatch.setattr(garmin_mod, "list_garmin_workouts", lambda uid: library)
    monkeypatch.setattr(
        garmin_mod, "get_garmin_workout_detail", lambda uid, wid: details[wid]
    )
    deleted = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", lambda uid, wid: deleted.append(wid))

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == ["aaa"]


def test_cleanup_adapted_workouts_skips_items_created_within_the_lookback_boundary(monkeypatch):
    """created "today" (or otherwise inside [start, end)) must NOT be swept —
    only createdDate <= yesterday, matching the original past-dated-only rule."""
    library = [_workout("aaa", "Jim · Legs")]
    monkeypatch.setattr(garmin_mod, "list_garmin_workouts", lambda uid: library)
    monkeypatch.setattr(
        garmin_mod, "get_garmin_workout_detail", lambda uid, wid: _detail("2026-07-10")
    )
    deleted = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", lambda uid, wid: deleted.append(wid))

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == []


def test_cleanup_adapted_workouts_a_delete_failure_does_not_stop_the_sweep(monkeypatch):
    library = [
        _workout("aaa", "Jim · Legs"),
        _workout("ccc", "Jim · Upper"),
    ]
    details = {"aaa": _detail("2026-07-01"), "ccc": _detail("2026-07-02")}
    monkeypatch.setattr(garmin_mod, "list_garmin_workouts", lambda uid: library)
    monkeypatch.setattr(
        garmin_mod, "get_garmin_workout_detail", lambda uid, wid: details[wid]
    )

    def maybe_boom(uid, wid):
        if wid == "aaa":
            raise RuntimeError("garmin down")
        deleted.append(wid)

    deleted: list[str] = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", maybe_boom)

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == ["ccc"]


def test_cleanup_adapted_workouts_a_missing_created_date_is_skipped_not_deleted(monkeypatch):
    """No date to compare against (missing/malformed detail) means "don't
    know if it's stale" — never guess and delete."""
    library = [_workout("aaa", "Jim · Legs")]
    monkeypatch.setattr(garmin_mod, "list_garmin_workouts", lambda uid: library)
    monkeypatch.setattr(garmin_mod, "get_garmin_workout_detail", lambda uid, wid: {})
    deleted = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", lambda uid, wid: deleted.append(wid))

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == []


def test_cleanup_adapted_workouts_zero_lookback_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(
        garmin_mod, "list_garmin_workouts",
        lambda uid: calls.append(uid) or [],
    )

    cleanup_adapted_workouts(1, date(2026, 7, 10), lookback_days=0)

    assert calls == []
