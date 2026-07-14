"""The load-bearing test of the Phase 3 (soft-baking-kettle) user_id threading:
proves two accounts' data never intersect. Offline (per CLAUDE.md) — a generic
fake Postgres stands in for `jim.db.connect()`/`jim.auth.connect()`, literal
enough to exercise the exact statements each function issues (same idiom as
tests/test_auth.py's FakeDB), not a real SQL engine.

A missed `WHERE user_id = %s` or a closure capturing the wrong id is a SILENT
cross-account leak, not a crash — that's why this file exists and why it tries
to be exhaustive rather than a token pass.
"""

import re
from datetime import date

import psycopg
import pytest

import jim.auth as auth_mod
import jim.db as db_mod
from jim.auth import create_user
from jim.db import kv_get, kv_set
from jim.playbook import Playbook, load_playbook, save_playbook
from jim.schemas import ExerciseStep, StructuredSession
from jim.tools.history import exercise_history, query_history, workout_history
from jim.tools.memory import chat_planned, record_outcome, record_suggestion

# --- a generic, literal fake Postgres ---------------------------------------
# Every user-scoped SELECT this system issues puts `user_id = %s` (or an
# aliased `x.user_id = %s`) first among its equality conditions, and every
# user-scoped INSERT lists `user_id` as a plain column — so one small engine
# can stand in for all of them without hardcoding each statement by hand.

_COND_RE = re.compile(r"(\w+(?:\.\w+)?)\s=\s%s")
_INSERT_RE = re.compile(r"INSERT INTO (\w+) \(([^)]+)\)\s+VALUES\s+\(([^)]+)\)(.*)")
_JSON_COLUMNS = {
    "kv": {"value"},
    "playbooks": {"rotation", "workouts", "pt_routines"},
    "suggestions": {"plan"},
    "notion_daily_log": {"habits"},
    "garmin_daily": {"raw"},
    "garmin_activities": {"summary"},
}


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._seq: dict[str, int] = {}

    def execute(self, sql: str, params=()) -> FakeCursor:
        s = " ".join(sql.split())
        params = tuple(params)
        if s.startswith("INSERT INTO"):
            return self._insert(s, params)
        if s.startswith("SELECT"):
            return self._select(s, params)
        raise NotImplementedError(sql)

    def _insert(self, s: str, params: tuple) -> FakeCursor:
        m = _INSERT_RE.match(s)
        assert m, f"fake can't parse insert: {s}"
        table, cols_str, placeholders_str, rest = m.groups()
        cols = [c.strip() for c in cols_str.split(",")]

        if table == "users" and any(
            r.get("email") == params[cols.index("email")] for r in self.tables.get(table, [])
        ):
            raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")

        values = []
        pi = 0
        for ph in [p.strip() for p in placeholders_str.split(",")]:
            if ph == "%s":
                values.append(params[pi])
                pi += 1
            else:
                values.append(None)  # now() or similar — irrelevant to isolation
        row = dict(zip(cols, values, strict=True))
        for col in _JSON_COLUMNS.get(table, ()):
            if isinstance(row.get(col), str):
                import json

                row[col] = json.loads(row[col])

        rows = self.tables.setdefault(table, [])
        conflict = re.search(r"ON CONFLICT \(([^)]+)\)", rest)
        if conflict and "DO UPDATE" in rest:
            keys = [k.strip() for k in conflict.group(1).split(",")]
            existing = next(
                (r for r in rows if all(r.get(k) == row.get(k) for k in keys)), None
            )
            if existing is not None:
                existing.update(row)
                row = existing
            else:
                rows.append(row)
        else:
            rows.append(row)

        if "RETURNING" in rest and "id" not in row:
            self._seq[table] = self._seq.get(table, 0) + 1
            row["id"] = self._seq[table]
        return FakeCursor([row])

    def _select(self, s: str, params: tuple) -> FakeCursor:
        table = re.search(r"FROM (\w+)", s).group(1)
        rows = list(self.tables.get(table, []))
        for i, col in enumerate(_COND_RE.findall(s)):
            col = col.split(".")[-1]
            if i < len(params):
                rows = [r for r in rows if r.get(col) == params[i]]
        if "LIMIT 1" in s:
            rows = rows[:1]
        return FakeCursor(rows)


class FakeConn:
    def __init__(self, db: FakeDB):
        self.db = db

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    # jim.db.connect is monkeypatched at the source: history.py/memory.py/
    # playbook.py/db.py all do `from jim.db import connect` INSIDE their
    # functions, so they pick up this patched attribute at call time. auth.py
    # imports `connect` at module scope, so it needs its own patch of the
    # same underlying db to actually share data with the rest.
    monkeypatch.setattr(db_mod, "connect", lambda: FakeConn(db))
    monkeypatch.setattr(auth_mod, "connect", lambda: FakeConn(db))
    monkeypatch.setattr(
        auth_mod, "settings", lambda: type("S", (), {"session_secret": "test-key"})()
    )
    return db


def _session(for_date: date, title: str) -> StructuredSession:
    return StructuredSession(
        for_date=for_date, kind="strength", title=title,
        steps=[ExerciseStep(exercise="Goblet squat", sets=3, reps=8)],
        est_duration_min=30, rationale_summary="test",
    )


def _insert_garmin_daily(db: FakeDB, user_id: int, day: date, readiness: int) -> None:
    with FakeConn(db) as conn:
        conn.execute(
            "INSERT INTO garmin_daily (user_id, day, readiness, body_battery)"
            " VALUES (%s, %s, %s, %s)",
            (user_id, day, readiness, readiness),
        )


def _insert_garmin_activity(db: FakeDB, user_id: int, activity_id: str, day: date) -> None:
    with FakeConn(db) as conn:
        conn.execute(
            "INSERT INTO garmin_activities (user_id, activity_id, day, type, duration_min)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, activity_id, day, "strength_training", 45.0),
        )


def _insert_notion_log(db: FakeDB, user_id: int, day: date, pain_level: int) -> None:
    with FakeConn(db) as conn:
        conn.execute(
            "INSERT INTO notion_daily_log (user_id, day, pain_level, pain_location, pain_notes)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user_id, day, pain_level, "knee", "note"),
        )


def _insert_exercise_set(db: FakeDB, user_id: int, activity_id: str, day: date) -> None:
    with FakeConn(db) as conn:
        conn.execute(
            "INSERT INTO exercise_sets (user_id, activity_id, set_index, day, category,"
            " exercise_name, reps, weight_kg) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, activity_id, 0, day, "SQUAT", "GOBLET_SQUAT", 8, 16.0),
        )


# --- kv ----------------------------------------------------------------------


def test_kv_isolated_across_users(fake_db):
    for key, value in [
        ("draft", [{"for_date": "2026-07-09", "title": "A"}]),
        ("goals", "user A's private goal"),
        ("chat_history", [{"role": "user", "content": "secret"}]),
        ("exercise_map", {"hip airplane": ["HIP_STABILITY", "HIP_CIRCLES"]}),
    ]:
        kv_set(1, key, value)
        assert kv_get(2, key) is None, f"user 2 saw user 1's {key!r}"
        assert kv_get(1, key) == value  # sanity: the owner still sees it


def test_kv_same_key_different_users_do_not_collide(fake_db):
    kv_set(1, "draft", "A's draft")
    kv_set(2, "draft", "B's draft")
    assert kv_get(1, "draft") == "A's draft"
    assert kv_get(2, "draft") == "B's draft"


# --- garmin/notion/exercise-set history rows ---------------------------------


def test_garmin_daily_row_invisible_across_users(fake_db):
    day = date(2026, 7, 10)
    _insert_garmin_daily(fake_db, 1, day, readiness=80)
    features = query_history(2, day, window_days=28)
    assert features.avg_readiness is None  # user 2's window is empty


def test_garmin_activity_and_exercise_set_rows_invisible_across_users(fake_db):
    day = date(2026, 7, 10)
    _insert_garmin_activity(fake_db, 1, "act-1", day)
    _insert_exercise_set(fake_db, 1, "act-1", day)

    features = query_history(2, day, window_days=28)
    assert features.weekly_volume_min == 0
    assert features.muscle_group_balance == {}

    summary = exercise_history(2, "goblet squat", days=180)
    assert "no logged sets" in summary
    # the owner still sees it, proving this is isolation, not a broken query
    assert "no logged sets" not in exercise_history(1, "goblet squat", days=180)


def test_notion_log_row_invisible_across_users(fake_db):
    day = date(2026, 7, 10)
    _insert_notion_log(fake_db, 1, day, pain_level=7)
    features = query_history(2, day, window_days=28)
    assert features.pain_trend == 0.0
    assert features.recent_pain_notes == []


# --- playbook ------------------------------------------------------------


def test_playbook_isolated_across_users(fake_db):
    pb_a = Playbook(rotation=["a"], directives="user A's private knee notes")
    save_playbook(1, pb_a)

    pb_b = load_playbook(2)
    assert pb_b.directives != "user A's private knee notes"
    assert pb_b == Playbook()  # user 2 has no row at all -> the safety-net default

    # the owner still gets their own content back
    assert load_playbook(1).directives == "user A's private knee notes"


def test_playbook_save_does_not_overwrite_another_users_row(fake_db):
    save_playbook(1, Playbook(directives="A"))
    save_playbook(2, Playbook(directives="B"))
    save_playbook(1, Playbook(directives="A revised"))
    assert load_playbook(1).directives == "A revised"
    assert load_playbook(2).directives == "B"


# --- suggestions / outcomes ---------------------------------------------------


def test_chat_planned_isolated_across_users(fake_db):
    target = date(2026, 7, 11)
    record_suggestion(
        1, target, _session(target, "A's plan"), "rationale", False, "fast", source="chat",
    )
    assert chat_planned(2, target) is False
    assert chat_planned(1, target) is True  # sanity


def test_workout_history_does_not_leak_another_users_adherence(fake_db):
    target = date(2026, 7, 11)
    sid = record_suggestion(
        1, target, _session(target, "A's plan"), "rationale", False, "fast", source="chat",
    )
    record_outcome(1, sid, actual_activity_id="act-1", adhered=True, notes="A's private note")

    history = workout_history(2, days=30)
    assert "A's private note" not in history
    assert "A's plan" not in history


def test_suggestions_and_outcomes_isolated_via_two_real_users(fake_db):
    """End-to-end with real signup, per the plan's suggested pattern."""
    user_a = create_user("athlete-a@example.com", "pw-a")
    user_b = create_user("athlete-b@example.com", "pw-b")
    assert user_a.id != user_b.id

    target = date(2026, 7, 12)
    record_suggestion(
        user_a.id, target, _session(target, "A's real plan"), "r", False, "fast",
        source="chat",
    )
    assert chat_planned(user_b.id, target) is False
    assert chat_planned(user_a.id, target) is True

    save_playbook(user_a.id, Playbook(directives="A's knee-specific notes"))
    # B's playbook is untouched by A's save — still whatever B was seeded with
    # at signup (the generic default, not A's content).
    assert load_playbook(user_b.id).directives != "A's knee-specific notes"

    kv_set(user_a.id, "goals", "A's real goal")
    assert kv_get(user_b.id, "goals") is None
