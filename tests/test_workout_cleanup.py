"""cleanup_stale_adaptations — sweeps one-off Garmin workouts (see coach.py's
_push_one and the "jim_created_workouts" kv entry) whose day has passed and
were never promoted into the playbook."""

from datetime import date

import jim.db as db
from jim.jobs.nightly import cleanup_stale_adaptations


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
