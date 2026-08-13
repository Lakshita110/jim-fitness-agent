"""backfill_if_empty (jobs/nightly.py): a brand-new signup has zero rows in
garmin_daily, so tonight's sync_today alone would leave the coach with only
today's data. This checks the emptiness gate and the skip path; the actual
day-by-day Garmin pull is exercised well enough by scripts/backfill.py's own
usage and test_nightly.py's fan-out coverage, so this stays focused on the
gate itself rather than re-testing Garmin fetch plumbing."""

from datetime import date

import jim.jobs.nightly as nightly_mod


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, has_history: bool):
        self.has_history = has_history
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.executed.append(s)
        if s.startswith("SELECT 1 FROM garmin_daily"):
            return FakeCursor([{"?column?": 1}] if self.has_history else [])
        if s.startswith("INSERT INTO"):
            return FakeCursor([])
        raise NotImplementedError(sql)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_skips_backfill_when_history_already_exists(monkeypatch):
    conn = FakeConn(has_history=True)
    monkeypatch.setattr(nightly_mod, "connect", lambda: conn)

    called = False

    def fake_get_garmin_today(user_id, day):
        nonlocal called
        called = True

    monkeypatch.setattr("jim.tools.garmin.get_garmin_today", fake_get_garmin_today)

    nightly_mod.backfill_if_empty(1, date(2026, 8, 13), days=90)

    assert called is False
    assert any(s.startswith("SELECT 1 FROM garmin_daily") for s in conn.executed)


def test_backfills_the_full_window_when_no_history_exists(monkeypatch):
    conn = FakeConn(has_history=False)
    monkeypatch.setattr(nightly_mod, "connect", lambda: conn)

    fetched_days = []

    class FakeSnapshot:
        hrv = None
        sleep_hours = None
        body_battery = None
        readiness = None
        resting_hr = None
        activities = []

        def model_dump_json(self):
            return "{}"

    def fake_get_garmin_today(user_id, day):
        fetched_days.append(day)
        return FakeSnapshot()

    monkeypatch.setattr("jim.tools.garmin.get_garmin_today", fake_get_garmin_today)

    nightly_mod.backfill_if_empty(1, date(2026, 8, 13), days=5)

    # days=5 means offsets 5..0 inclusive -> 6 days pulled.
    assert len(fetched_days) == 6
    assert fetched_days[0] == date(2026, 8, 8)
    assert fetched_days[-1] == date(2026, 8, 13)
