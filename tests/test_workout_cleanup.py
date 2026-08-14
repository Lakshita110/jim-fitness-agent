"""cleanup_adapted_workouts: sweeps one-off Garmin workouts Claude created via
mcp_server.create_or_update_workout, by reading Garmin's own scheduled-
workouts calendar for the ADAPTED_WORKOUT_PREFIX marker on past-dated items —
there's no Jim-side record of having created them, so Garmin's calendar is
the only source of truth."""

from datetime import date

import jim.tools.garmin as garmin_mod
from jim.jobs.nightly import cleanup_adapted_workouts


def test_cleanup_adapted_workouts_deletes_only_prefixed_past_items(monkeypatch):
    scheduled = [
        {"date": date(2026, 7, 1), "workout_id": "aaa", "title": "Jim · Legs — sore knee"},
        {"date": date(2026, 7, 5), "workout_id": "bbb", "title": "Full Body A"},  # not ours
    ]
    monkeypatch.setattr(
        garmin_mod, "get_scheduled_workouts", lambda uid, start, end: scheduled
    )
    deleted = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", lambda uid, wid: deleted.append(wid))

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == ["aaa"]


def test_cleanup_adapted_workouts_a_delete_failure_does_not_stop_the_sweep(monkeypatch):
    scheduled = [
        {"date": date(2026, 7, 1), "workout_id": "aaa", "title": "Jim · Legs"},
        {"date": date(2026, 7, 2), "workout_id": "ccc", "title": "Jim · Upper"},
    ]
    monkeypatch.setattr(
        garmin_mod, "get_scheduled_workouts", lambda uid, start, end: scheduled
    )

    def maybe_boom(uid, wid):
        if wid == "aaa":
            raise RuntimeError("garmin down")
        deleted.append(wid)

    deleted: list[str] = []
    monkeypatch.setattr(garmin_mod, "delete_garmin_workout", maybe_boom)

    cleanup_adapted_workouts(1, date(2026, 7, 10))

    assert deleted == ["ccc"]


def test_cleanup_adapted_workouts_zero_lookback_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(
        garmin_mod, "get_scheduled_workouts",
        lambda uid, start, end: calls.append((start, end)) or [],
    )

    cleanup_adapted_workouts(1, date(2026, 7, 10), lookback_days=0)

    assert calls == []
