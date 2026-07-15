from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jim.app as app_mod
from jim import auth, coach
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
    with `coach`/`auth` mocked, so the schema-migration step is not under
    test. Left real, it calls db.ensure_migrated() -> connect(), which
    hard-raises without a live DATABASE_URL and turns every DB-backed route
    into a bare 500."""
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


def test_chat_page_redirects_to_login_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_chat_page_serves_when_authenticated(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    ok = client.get("/chat")
    assert ok.status_code == 200
    assert "Jim" in ok.text


def test_login_page_is_public():
    r = client.get("/login")
    assert r.status_code == 200
    assert "Jim" in r.text


def test_chat_message_flow(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(
        coach, "converse",
        lambda text, user_id, scope_date=None: {
            "reply": f"echo: {text}", "draft": [], "scope": scope_date,
        },
    )
    # No cookie -> shut, even for a well-formed body.
    assert client.post("/chat/message", json={"text": "knee sore"}).status_code == 403

    _sign_in(monkeypatch)
    r = client.post("/chat/message", json={"text": "knee sore"})
    assert r.status_code == 200
    assert r.json() == {"reply": "echo: knee sore", "draft": [], "scope": None}
    # scope_date is threaded through to the coach
    r2 = client.post("/chat/message", json={"text": "easier", "scope_date": "2026-07-14"})
    assert r2.json()["scope"] == "2026-07-14"
    assert client.post("/chat/message", json={"text": "  "}).status_code == 400


def test_chat_approve_clear_state(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(
        coach, "approve", lambda user_id: "Pushed to Garmin:\n2026-07-09: ok"
    )
    cleared = []
    monkeypatch.setattr(coach, "clear", lambda user_id: cleared.append(True))
    monkeypatch.setattr(
        coach, "current_state",
        lambda user_id: {"history": [], "draft": [], "goals": "g", "push_status": {}},
    )

    assert client.post("/chat/approve", json={}).status_code == 403  # no cookie

    _sign_in(monkeypatch)
    r = client.post("/chat/approve", json={})
    assert r.status_code == 200 and "Pushed" in r.json()["summary"]
    assert r.json()["push_status"] == {}

    assert client.post("/chat/clear", json={}).json() == {"ok": True}
    assert cleared == [True]

    s = client.get("/chat/state")
    assert s.json()["goals"] == "g"


def test_chat_state_requires_cookie(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    assert client.get("/chat/state").status_code == 403


def test_chat_push_day(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    seen = {}
    monkeypatch.setattr(
        coach, "push_day",
        lambda date, user_id: seen.update(date=date) or {
            "summary": f"Pushed {date}", "draft": [], "push_status": {date: "pushed"},
        },
    )
    r0 = client.post("/chat/push-day", json={"date": "2026-07-14"})
    assert r0.status_code == 403  # no cookie

    _sign_in(monkeypatch)
    r = client.post("/chat/push-day", json={"date": "2026-07-14"})
    assert r.status_code == 200
    assert r.json()["summary"] == "Pushed 2026-07-14"
    assert r.json()["push_status"] == {"2026-07-14": "pushed"}


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
    assert r.status_code == 200 and r.json() == {"ok": True}
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
    assert r.status_code == 200 and r.json() == {"ok": True}
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
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    assert client.get("/chat/state").status_code != 403  # signed in first

    r = client.post("/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}

    assert client.get("/chat/state").status_code == 403


def test_forged_cookie_resolves_to_unauthenticated_not_a_crash(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    client.cookies.set(auth.SESSION_COOKIE_NAME, "not-a-real-token")
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"  # bounced, no crash
    assert client.get("/chat/state").status_code == 403


def test_manifest_is_public_and_carries_no_secret(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200                     # installable without signing in
    assert r.json()["start_url"] == "/chat"          # clean start_url
