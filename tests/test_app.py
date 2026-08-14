from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jim.app as app_mod
from jim import auth
from jim.auth import User
from jim.web import deps

client = TestClient(app_mod.app)

TEST_USER = User(id=1, email="athlete@example.com")


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    """A session cookie persists across requests in TestClient's cookie jar, so
    without this, one test's sign-in would silently authenticate the next
    test's "no cookie -> 403" assertions.

    Also stub `_ready()`: these are unit tests of routing/auth/serialization
    with `auth` mocked, so the schema-migration step is not under test. Left
    real, it calls db.ensure_migrated() -> connect(), which hard-raises
    without a live DATABASE_URL and turns every DB-backed route into a bare
    500."""
    monkeypatch.setattr(deps, "_ready", lambda: None)
    client.cookies.clear()
    yield
    client.cookies.clear()


def fake_settings():
    return SimpleNamespace(app_timezone="America/New_York", cron_secret="cr0n")


def _sign_in(monkeypatch, c=client, user=TEST_USER):
    """Fake login: stub out real password/DB auth, then go through the actual
    /auth/login route so the cookie is set the same way a browser's would be
    (TestClient's cookie jar only reliably tracks cookies it received via a
    Set-Cookie response header, not ones poked into the jar directly)."""
    monkeypatch.setattr(auth, "authenticate", lambda email, password: user)
    monkeypatch.setattr(
        auth, "get_user_by_id", lambda uid: user if uid == user.id else None
    )
    r = c.post("/auth/login", json={"email": user.email, "password": "irrelevant"})
    assert r.status_code == 200, r.text


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_constraints_roundtrip(monkeypatch):
    import jim.db as db_mod

    monkeypatch.setattr(app_mod, "settings", fake_settings)
    store: dict[int, str] = {}
    monkeypatch.setattr(db_mod, "get_constraints", lambda uid: store.get(uid, ""))
    monkeypatch.setattr(
        db_mod, "set_constraints", lambda uid, content: store.__setitem__(uid, content)
    )

    # No cookie -> shut, even for a well-formed body.
    assert client.get("/api/constraints").status_code == 403
    assert client.post("/api/constraints", json={"content": "x"}).status_code == 403

    _sign_in(monkeypatch)
    r = client.post("/api/constraints", json={"content": "no jumping, knee PT daily"})
    assert r.status_code == 200 and r.json() == {"ok": True}

    r2 = client.get("/api/constraints")
    assert r2.status_code == 200
    assert r2.json() == {"content": "no jumping, knee PT daily"}


def test_cron_nightly_requires_vercel_bearer(monkeypatch):
    """Vercel Cron authenticates with `Authorization: Bearer $CRON_SECRET`. An
    unauthenticated endpoint would let anyone burn LLM spend and rewrite the plan."""
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    import jim.jobs.nightly as nightly_mod

    ran = []
    monkeypatch.setattr(
        nightly_mod, "run_nightly",
        lambda: ran.append(True)
        or {"users": {1: {"for_date": "2026-07-13"}}, "elapsed_sec": 12.3},
    )

    assert client.get("/api/cron/nightly").status_code == 403
    assert client.get(
        "/api/cron/nightly", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403
    assert ran == []  # neither attempt executed the job

    r = client.get("/api/cron/nightly", headers={"Authorization": "Bearer cr0n"})
    assert r.status_code == 200
    assert r.json()["elapsed_sec"] == 12.3
    assert r.json()["users"] == {"1": {"for_date": "2026-07-13"}}
    assert ran == [True]


def test_cron_nightly_shut_when_no_secret_configured(monkeypatch):
    """No CRON_SECRET => endpoint stays closed, rather than defaulting to open."""
    monkeypatch.setattr(
        app_mod, "settings",
        lambda: SimpleNamespace(app_timezone="UTC", cron_secret=""),
    )
    assert client.get(
        "/api/cron/nightly", headers={"Authorization": "Bearer "}
    ).status_code == 403


# --- /auth/* routes -------------------------------------------------------------


def test_signup_creates_user_and_sets_cookie(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(auth, "create_user", lambda email, password: TEST_USER)
    r = client.post("/auth/signup", json={"email": "athlete@example.com", "password": "hunter2"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert auth.verify_session_token(r.json()["token"]) == TEST_USER.id
    assert auth.SESSION_COOKIE_NAME in r.cookies


def test_signup_duplicate_email_returns_400_with_message(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)

    def raise_dup(email, password):
        raise ValueError("an account with this email already exists")

    monkeypatch.setattr(auth, "create_user", raise_dup)
    r = client.post("/auth/signup", json={"email": "dup@example.com", "password": "x"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]
    assert auth.SESSION_COOKIE_NAME not in r.cookies


def test_login_success_sets_cookie(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(auth, "authenticate", lambda email, password: TEST_USER)
    r = client.post("/auth/login", json={"email": "athlete@example.com", "password": "hunter2"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert auth.verify_session_token(r.json()["token"]) == TEST_USER.id
    assert auth.SESSION_COOKIE_NAME in r.cookies


def test_login_failure_is_generic_for_wrong_password_and_unknown_email(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(auth, "authenticate", lambda email, password: None)

    wrong_pw = client.post("/auth/login", json={"email": "athlete@example.com", "password": "bad"})
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "x"})

    assert wrong_pw.status_code == 401 and unknown.status_code == 401
    assert wrong_pw.json() == unknown.json() == {"detail": "invalid email or password"}
    assert auth.SESSION_COOKIE_NAME not in wrong_pw.cookies
    assert auth.SESSION_COOKIE_NAME not in unknown.cookies


def test_logout_clears_cookie_and_subsequent_requests_are_unauthenticated(monkeypatch):
    import jim.db as db_mod

    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(db_mod, "get_constraints", lambda uid: "")
    _sign_in(monkeypatch)
    assert client.get("/api/constraints").status_code != 403  # signed in first

    r = client.post("/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}

    assert client.get("/api/constraints").status_code == 403


def test_forged_cookie_resolves_to_unauthenticated_not_a_crash(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    client.cookies.set(auth.SESSION_COOKIE_NAME, "not-a-real-token")
    assert client.get("/api/constraints").status_code == 403  # bounced, no crash
