"""auth.py — offline (no live Postgres, per CLAUDE.md). A minimal in-memory
fake stands in for jim.db.connect(), matching auth.py's SQL statements by
prefix. auth.py owns those statements, so the fake is allowed to be this
literal — it isn't re-implementing Postgres, just enough of it to exercise
create_user/get_user_by_email/get_user_by_id."""

from types import SimpleNamespace

import psycopg
import pytest

import jim.auth as auth_mod
from jim.auth import User


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self):
        self.users = []
        self.user_credentials = {}
        self.playbooks = {}
        self._next_id = 1

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO users"):
            email, password_hash = params
            if any(u["email"] == email for u in self.users):
                raise psycopg.errors.UniqueViolation(
                    'duplicate key value violates unique constraint "users_email_key"'
                )
            uid = self._next_id
            self._next_id += 1
            row = {"id": uid, "email": email, "password_hash": password_hash}
            self.users.append(row)
            return FakeCursor([{"id": uid, "email": email}])
        if s.startswith("INSERT INTO user_credentials"):
            (user_id,) = params
            self.user_credentials[user_id] = {"user_id": user_id}
            return FakeCursor([])
        if s.startswith("INSERT INTO playbooks"):
            user_id = params[0]
            self.playbooks[user_id] = {"user_id": user_id, "raw": params}
            return FakeCursor([])
        if s.startswith("SELECT id, email, password_hash FROM users WHERE email"):
            (email,) = params
            match = next((u for u in self.users if u["email"] == email), None)
            return FakeCursor([match] if match else [])
        if s.startswith("SELECT id, email FROM users WHERE id"):
            (user_id,) = params
            match = next((u for u in self.users if u["id"] == user_id), None)
            row = {"id": match["id"], "email": match["email"]} if match else None
            return FakeCursor([row] if row else [])
        raise NotImplementedError(sql)


class FakeConn:
    def __init__(self, db):
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
    monkeypatch.setattr(auth_mod, "connect", lambda: FakeConn(db))
    monkeypatch.setattr(
        auth_mod, "settings", lambda: SimpleNamespace(session_secret="test-signing-key")
    )
    return db


# --- signup / create_user ----------------------------------------------------


def test_create_user_hashes_password_and_seeds_rows(fake_db):
    user = auth_mod.create_user("Athlete@Example.com", "hunter2")
    assert isinstance(user, User)
    assert user.email == "athlete@example.com"  # lowercased/stripped

    stored = fake_db.users[0]
    assert stored["password_hash"] != "hunter2"  # never the plaintext
    assert auth_mod.verify_password("hunter2", stored["password_hash"])

    assert user.id in fake_db.user_credentials
    assert user.id in fake_db.playbooks


def test_create_user_seeds_the_generic_default_playbook_not_bare_empty(fake_db):
    """Phase 4: new signups get playbook/defaults/ content (a placeholder
    directives sentence), not an empty-empty literal and not the committed
    athlete's real content — prove it's actually read from disk."""
    from jim.playbook import _load_default_playbook

    user = auth_mod.create_user("mom@example.com", "pw")
    _, rotation_json, workouts_json, pt_json, directives = fake_db.playbooks[user.id]["raw"]

    default = _load_default_playbook()
    assert directives == default.directives
    assert "Settings" in directives and "Playbook" in directives
    # not the real athlete's own content
    assert "knee" not in directives.lower()
    assert workouts_json == "{}"
    assert rotation_json == "[]"


def test_create_user_duplicate_email_rejected(fake_db):
    auth_mod.create_user("dup@example.com", "pw1")
    with pytest.raises(ValueError, match="already exists"):
        auth_mod.create_user("dup@example.com", "pw2")
    # case-insensitive uniqueness too
    with pytest.raises(ValueError, match="already exists"):
        auth_mod.create_user("Dup@Example.com", "pw3")
    assert len(fake_db.users) == 1


def test_create_user_email_normalized_before_uniqueness_check(fake_db):
    auth_mod.create_user("  Same@Example.com  ", "pw")
    with pytest.raises(ValueError):
        auth_mod.create_user("same@example.com", "pw2")


# --- lookups / authenticate ---------------------------------------------------


def test_get_user_by_email_and_id(fake_db):
    created = auth_mod.create_user("look@example.com", "pw")
    assert auth_mod.get_user_by_email("look@example.com") == User(
        id=created.id, email=created.email
    )
    assert auth_mod.get_user_by_email("LOOK@example.com").id == created.id
    assert auth_mod.get_user_by_email("nope@example.com") is None

    assert auth_mod.get_user_by_id(created.id) == created
    assert auth_mod.get_user_by_id(999999) is None


def test_authenticate_success(fake_db):
    created = auth_mod.create_user("login@example.com", "correct-horse")
    user = auth_mod.authenticate("login@example.com", "correct-horse")
    assert user == created


def test_authenticate_generic_failure_wrong_password_and_unknown_email(fake_db):
    auth_mod.create_user("real@example.com", "correct-horse")

    wrong_pw = auth_mod.authenticate("real@example.com", "wrong")
    unknown_email = auth_mod.authenticate("ghost@example.com", "whatever")

    # Both failure paths look identical to the caller: None either way, so
    # nothing lets you distinguish "wrong password" from "no such account".
    assert wrong_pw is None
    assert unknown_email is None


# --- session tokens ------------------------------------------------------------


def test_session_token_roundtrip(fake_db):
    token = auth_mod.create_session_token(42)
    assert auth_mod.verify_session_token(token) == 42


def test_session_token_tampered_returns_none(fake_db):
    token = auth_mod.create_session_token(42)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth_mod.verify_session_token(tampered) is None


def test_session_token_garbage_returns_none(fake_db):
    assert auth_mod.verify_session_token("not-a-real-token") is None
    assert auth_mod.verify_session_token("") is None


def test_session_token_expired_returns_none(fake_db, monkeypatch):
    token = auth_mod.create_session_token(7)
    monkeypatch.setattr(auth_mod, "SESSION_MAX_AGE", -1)  # already expired
    assert auth_mod.verify_session_token(token) is None
