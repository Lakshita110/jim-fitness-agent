"""Garmin web onboarding (soft-baking-kettle plan Phase 4): /settings/garmin and
the two-step MFA connect flow. Offline per CLAUDE.md — garminconnect.Garmin is
faked, never a real network call."""

from types import SimpleNamespace

import garminconnect
import pytest
from fastapi.testclient import TestClient

import jim.app as app_mod
import jim.db as db_mod
import jim.web.garmin_routes as garmin_routes
from jim import auth
from jim.auth import User
from jim.web import deps

client = TestClient(app_mod.app)
TEST_USER = User(id=7, email="mom@example.com")


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    monkeypatch.setattr(deps, "_ready", lambda: None)
    client.cookies.clear()
    yield
    client.cookies.clear()
    garmin_routes._pending_garmin_logins.clear()


def fake_settings():
    return SimpleNamespace(app_timezone="America/New_York", cron_secret="cr0n")


def _sign_in(monkeypatch, user=TEST_USER):
    monkeypatch.setattr(auth, "authenticate", lambda email, password: user)
    monkeypatch.setattr(
        auth, "get_user_by_id", lambda uid: user if uid == user.id else None
    )
    r = client.post("/auth/login", json={"email": user.email, "password": "irrelevant"})
    assert r.status_code == 200, r.text


class FakeClient:
    """Stands in for garminconnect's internal `Client`, only the `.dumps()`
    call the connect flow needs (same mechanism scripts/garmin_login.py
    --export uses)."""

    def dumps(self):
        return "x" * 600  # > MIN_TOKEN_BLOB_CHARS


class FakeGarminOK:
    def __init__(self, email, password, return_on_mfa=False, **kw):
        self.username, self.password = email, password
        self.client = FakeClient()

    def login(self):
        return None, None


class FakeGarminMFA:
    def __init__(self, email, password, return_on_mfa=False, **kw):
        self.username, self.password = email, password
        self.client = FakeClient()
        self.resumed = False

    def login(self):
        return "needs_mfa", None

    def resume_login(self, client_state, mfa_code):
        if mfa_code != "123456":
            raise garminconnect.GarminConnectAuthenticationError("bad mfa code")
        self.resumed = True
        return None, None


class FakeGarminBadPassword:
    def __init__(self, email, password, return_on_mfa=False, **kw):
        pass

    def login(self):
        raise garminconnect.GarminConnectAuthenticationError("Authentication failed (401)")


def test_settings_garmin_page_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.get("/settings/garmin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_connect_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.post(
        "/settings/garmin/connect",
        json={"garmin_email": "a@b.com", "garmin_password": "x"},
    )
    assert r.status_code == 403


def test_mfa_endpoint_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.post("/settings/garmin/mfa", json={"mfa_code": "000000"})
    assert r.status_code == 403


def test_successful_connect_saves_credentials_for_the_right_user(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarminOK)
    saved = {}
    monkeypatch.setattr(
        db_mod, "save_user_credentials",
        lambda user_id, **fields: saved.update(user_id=user_id, **fields),
    )

    r = client.post(
        "/settings/garmin/connect",
        json={"garmin_email": "mom@example.com", "garmin_password": "hunter2"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert saved["user_id"] == TEST_USER.id
    assert saved["garmin_email"] == "mom@example.com"
    assert saved["garmin_password"] == "hunter2"
    assert len(saved["garmin_tokens"]) > 512


def test_mfa_challenge_then_resume_completes_and_saves(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarminMFA)
    saved = {}
    monkeypatch.setattr(
        db_mod, "save_user_credentials",
        lambda user_id, **fields: saved.update(user_id=user_id, **fields),
    )

    r = client.post(
        "/settings/garmin/connect",
        json={"garmin_email": "mom@example.com", "garmin_password": "hunter2"},
    )
    assert r.status_code == 200
    assert r.json() == {"mfa_required": True}
    assert saved == {}  # nothing persisted yet — still mid-flow

    bad = client.post("/settings/garmin/mfa", json={"mfa_code": "000000"})
    assert bad.status_code == 400
    assert saved == {}

    # The bad attempt must not have consumed/cleared the pending login.
    ok = client.post("/settings/garmin/mfa", json={"mfa_code": "123456"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    assert saved["user_id"] == TEST_USER.id
    assert saved["garmin_email"] == "mom@example.com"


def test_mfa_resume_without_a_pending_login_returns_400_not_500(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    r = client.post("/settings/garmin/mfa", json={"mfa_code": "123456"})
    assert r.status_code == 400


def test_status_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.get("/api/garmin/status")
    assert r.status_code == 403


def test_status_reports_not_connected_before_any_credentials(monkeypatch):
    """The bug this guards: /settings/garmin always rendered a blank form, so
    landing here right after a successful connect looked identical to never
    having connected at all — nothing distinguished the two states."""
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(db_mod, "get_user_credentials", lambda user_id: None)

    r = client.get("/api/garmin/status")
    assert r.status_code == 200
    assert r.json() == {"connected": False, "garmin_email": None}


def test_status_reports_connected_after_credentials_exist(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(
        db_mod, "get_user_credentials",
        lambda user_id: {
            "garmin_email": "mom@example.com", "garmin_tokens": "x" * 600,
            "garmin_password": "hunter2",
        } if user_id == TEST_USER.id else None,
    )

    r = client.get("/api/garmin/status")
    assert r.status_code == 200
    assert r.json() == {"connected": True, "garmin_email": "mom@example.com"}


def test_status_is_not_connected_with_only_a_credentials_row_and_no_secrets(monkeypatch):
    """Every user gets an empty user_credentials row at signup (Phase 2) — a
    row existing must not be mistaken for a working connection."""
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(
        db_mod, "get_user_credentials",
        lambda user_id: {
            "garmin_email": None, "garmin_tokens": None, "garmin_password": None,
        },
    )

    r = client.get("/api/garmin/status")
    assert r.json()["connected"] is False


def test_wrong_password_returns_clear_error_not_500(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    monkeypatch.setattr(garminconnect, "Garmin", FakeGarminBadPassword)
    saved_calls = []
    monkeypatch.setattr(
        db_mod, "save_user_credentials", lambda user_id, **f: saved_calls.append(user_id)
    )

    r = client.post(
        "/settings/garmin/connect",
        json={"garmin_email": "mom@example.com", "garmin_password": "wrong"},
    )
    assert r.status_code == 400
    assert "detail" in r.json()
    assert saved_calls == []


def test_connect_page_states_password_is_stored(monkeypatch):
    """The trust-note requirement: visible on the form, not buried."""
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    r = client.get("/settings/garmin")
    assert r.status_code == 200
    assert "store your Garmin password, encrypted" in r.text
