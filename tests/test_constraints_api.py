"""/api/constraints — the small per-athlete free-text record (knee/ankle
limits, safety rules, goals) that replaced the playbook's template library.
Also exercises the bearer-token auth path, since that's how the MCP server
(no cookie jar) authenticates."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jim.app as app_mod
from jim import auth, db
from jim.auth import User
from jim.web import deps

client = TestClient(app_mod.app)
TEST_USER = User(id=7, email="athlete@example.com")


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    monkeypatch.setattr(deps, "_ready", lambda: None)
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    client.cookies.clear()
    yield
    client.cookies.clear()


def fake_settings():
    return SimpleNamespace(app_timezone="America/New_York", cron_secret="cr0n")


def _sign_in(monkeypatch, user=TEST_USER):
    monkeypatch.setattr(auth, "authenticate", lambda email, password: user)
    monkeypatch.setattr(
        auth, "get_user_by_id", lambda uid: user if uid == user.id else None
    )
    r = client.post("/auth/login", json={"email": user.email, "password": "irrelevant"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_and_signup_return_a_bearer_token(monkeypatch):
    monkeypatch.setattr(auth, "create_user", lambda email, password: TEST_USER)
    r = client.post(
        "/auth/signup", json={"email": "new@example.com", "password": "hunter2"}
    )
    assert r.status_code == 200
    token = r.json()["token"]
    assert isinstance(token, str) and token
    assert auth.verify_session_token(token) == TEST_USER.id


def test_constraints_requires_auth():
    assert client.get("/api/constraints").status_code == 403
    assert client.post("/api/constraints", json={"content": "x"}).status_code == 403


def test_constraints_round_trip_via_cookie(monkeypatch):
    store: dict[int, str] = {}
    monkeypatch.setattr(db, "get_constraints", lambda uid: store.get(uid, ""))
    monkeypatch.setattr(db, "set_constraints", lambda uid, content: store.__setitem__(uid, content))

    _sign_in(monkeypatch)
    assert client.get("/api/constraints").json() == {"content": ""}

    r = client.post("/api/constraints", json={"content": "no jump squats; knee PT daily"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/constraints").json() == {
        "content": "no jump squats; knee PT daily"
    }


def test_constraints_accessible_via_bearer_token_with_no_cookie(monkeypatch):
    """The MCP server has no cookie jar — it authenticates purely with the
    token minted at login, sent as `Authorization: Bearer <token>`."""
    monkeypatch.setattr(db, "get_constraints", lambda uid: "constraints for " + str(uid))
    token = _sign_in(monkeypatch)
    client.cookies.clear()  # simulate a client that never had a cookie at all

    r = client.get("/api/constraints", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"content": f"constraints for {TEST_USER.id}"}


def test_bearer_token_bad_or_missing_is_unauthenticated():
    r = client.get("/api/constraints", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 403
    bad = client.get("/api/constraints", headers={"Authorization": "not-even-bearer x"})
    assert bad.status_code == 403
