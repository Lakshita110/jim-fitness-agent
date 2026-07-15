"""jobs/nightly.py's fan-out (soft-baking-kettle Phase 5): run_nightly() must
iterate every nightly_enabled user, isolate one user's failure from the rest,
and skip users with nightly_enabled=false. Offline: connect()/ensure_migrated()
are monkeypatched, and _run_nightly_for_user is stubbed per-test so these tests
exercise the fan-out logic itself, not the full sync/reconcile/cleanup pipeline
(that's covered by test_reconcile.py, test_workout_cleanup.py, etc)."""

import jim.jobs.nightly as nightly_mod


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, users):
        self._users = users

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if "WHERE nightly_enabled = true" in s:
            return FakeCursor([{"id": u["id"]} for u in self._users if u["nightly_enabled"]])
        if s.startswith("SELECT timezone FROM users WHERE id = %s"):
            (uid,) = params
            match = next((u for u in self._users if u["id"] == uid), None)
            return FakeCursor([{"timezone": match["timezone"]}] if match else [])
        raise NotImplementedError(sql)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch(monkeypatch, users):
    conn = FakeConn(users)
    monkeypatch.setattr(nightly_mod, "connect", lambda: conn)
    monkeypatch.setattr(nightly_mod, "ensure_migrated", lambda: None)


def test_run_nightly_isolates_one_users_failure_from_the_rest(monkeypatch):
    users = [
        {"id": 1, "nightly_enabled": True, "timezone": "America/New_York"},
        {"id": 2, "nightly_enabled": True, "timezone": "America/New_York"},
    ]
    _patch(monkeypatch, users)

    def fake_run(user_id):
        if user_id == 2:
            raise RuntimeError("expired Garmin session")
        return {"for_date": "2026-07-15", "elapsed_sec": 1.0}

    monkeypatch.setattr(nightly_mod, "_run_nightly_for_user", fake_run)

    result = nightly_mod.run_nightly()

    assert result["users"][1] == {"for_date": "2026-07-15", "elapsed_sec": 1.0}
    assert result["users"][2] == {"error": True}
    assert "elapsed_sec" in result


def test_run_nightly_excludes_nightly_disabled_users(monkeypatch):
    users = [
        {"id": 1, "nightly_enabled": True, "timezone": "America/New_York"},
        {"id": 2, "nightly_enabled": False, "timezone": "America/New_York"},
    ]
    _patch(monkeypatch, users)

    monkeypatch.setattr(
        nightly_mod, "_run_nightly_for_user",
        lambda user_id: {"for_date": "2026-07-15", "elapsed_sec": 1.0},
    )

    result = nightly_mod.run_nightly()

    assert 1 in result["users"]
    assert 2 not in result["users"]


def test_today_for_user_reads_per_user_timezone(monkeypatch):
    users = [{"id": 1, "nightly_enabled": True, "timezone": "UTC"}]
    _patch(monkeypatch, users)
    # Just confirm it resolves without error and returns a date using the
    # user's own timezone column rather than raising / hanging.
    today = nightly_mod._today_for_user(1)
    assert today is not None


def test_today_for_user_falls_back_to_app_timezone_when_column_empty(monkeypatch):
    users = [{"id": 1, "nightly_enabled": True, "timezone": None}]
    _patch(monkeypatch, users)
    monkeypatch.setattr(
        nightly_mod, "settings", lambda: type("S", (), {"app_timezone": "UTC"})()
    )
    today = nightly_mod._today_for_user(1)
    assert today is not None
