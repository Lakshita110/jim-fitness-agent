"""cleanup_stale_adaptations — sweeps one-off Garmin workouts (see coach.py's
_push_one and the "jim_created_workouts" kv entry) whose day has passed and
were never promoted into the playbook.

cleanup_adapted_workouts is the MCP path's equivalent — same idea, but with
no kv bookkeeping to read (Claude creates workouts directly, with nothing
recording that it did), so it sweeps by reading Garmin's own scheduled-
workouts calendar for the ADAPTED_WORKOUT_PREFIX marker instead."""

from datetime import date

import jim.db as db
import jim.tools.garmin as garmin_mod
from jim.jobs.nightly import cleanup_adapted_workouts, cleanup_stale_adaptations


def test_deletes_past_dated_entries_and_leaves_future_ones(monkeypatch):
    store = {
        (1, "jim_created_workouts"): {
            "2026-07-01": {"workout_id": "aaa", "template_key": "full_body_a"},
            "2026-07-20": {"workout_id": "bbb", "template_key": None},
        }
    }
    monkeypatch.setattr(db, "kv_get", lambda uid, key: store.get((uid, key)))
    monkeypatch.setattr(db, "kv_set", lambda uid, key, value: store.__setitem__((uid, key), value))
    deleted = []
    monkeypatch.setattr("jim.tools.garmin.delete_garmin_workout",
                        lambda uid, wid: deleted.append(wid))

    cleanup_stale_adaptations(1, date(2026, 7, 10))

    assert deleted == ["aaa"]
    remaining = store[(1, "jim_created_workouts")]
    assert set(remaining) == {"2026-07-20"}


def test_a_delete_failure_leaves_the_entry_for_retry(monkeypatch):
    store = {
        (1, "jim_created_workouts"): {
            "2026-07-01": {"workout_id": "aaa", "template_key": None},
        }
    }
    monkeypatch.setattr(db, "kv_get", lambda uid, key: store.get((uid, key)))
    monkeypatch.setattr(db, "kv_set", lambda uid, key, value: store.__setitem__((uid, key), value))

    def boom(uid, wid):
        raise RuntimeError("garmin down")

    monkeypatch.setattr("jim.tools.garmin.delete_garmin_workout", boom)

    cleanup_stale_adaptations(1, date(2026, 7, 10))

    remaining = store[(1, "jim_created_workouts")]
    assert remaining == {"2026-07-01": {"workout_id": "aaa", "template_key": None}}


def test_no_entries_is_a_noop(monkeypatch):
    monkeypatch.setattr(db, "kv_get", lambda uid, key: None)
    calls = []
    monkeypatch.setattr(db, "kv_set", lambda uid, key, value: calls.append(value))

    cleanup_stale_adaptations(1, date(2026, 7, 10))

    assert calls == [{}]


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
