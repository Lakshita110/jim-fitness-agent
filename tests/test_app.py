from types import SimpleNamespace

from fastapi.testclient import TestClient

import jim.app as app_mod

client = TestClient(app_mod.app)


def fake_settings():
    return SimpleNamespace(
        chat_secret="s3cret", app_timezone="America/New_York", cron_secret="cr0n"
    )


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_chat_page_requires_key(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    assert client.get("/chat").status_code == 403
    assert client.get("/chat", params={"key": "wrong"}).status_code == 403
    ok = client.get("/chat", params={"key": "s3cret"})
    assert ok.status_code == 200
    assert "Jim" in ok.text


def test_chat_disabled_without_secret(monkeypatch):
    monkeypatch.setattr(
        app_mod, "settings",
        lambda: SimpleNamespace(chat_secret="", app_timezone="America/New_York"),
    )
    # no secret configured -> chat is off, even with an empty key
    assert client.get("/chat", params={"key": ""}).status_code == 403


def test_chat_message_flow(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(
        app_mod.coach, "converse",
        lambda text, scope_date=None: {"reply": f"echo: {text}", "draft": [], "scope": scope_date},
    )
    r = client.post("/chat/message", json={"key": "s3cret", "text": "knee sore"})
    assert r.status_code == 200
    assert r.json() == {"reply": "echo: knee sore", "draft": [], "scope": None}
    # scope_date is threaded through to the coach
    r2 = client.post("/chat/message",
                     json={"key": "s3cret", "text": "easier", "scope_date": "2026-07-14"})
    assert r2.json()["scope"] == "2026-07-14"
    assert client.post("/chat/message", json={"key": "bad", "text": "x"}).status_code == 403
    assert client.post("/chat/message", json={"key": "s3cret", "text": "  "}).status_code == 400


def test_chat_approve_clear_state(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    monkeypatch.setattr(app_mod.coach, "approve", lambda: "Pushed to Garmin:\n2026-07-09: ok")
    cleared = []
    monkeypatch.setattr(app_mod.coach, "clear", lambda: cleared.append(True))
    monkeypatch.setattr(
        app_mod.coach, "current_state",
        lambda: {"history": [], "draft": [], "goals": "g", "push_status": {}},
    )

    r = client.post("/chat/approve", json={"key": "s3cret"})
    assert r.status_code == 200 and "Pushed" in r.json()["summary"]
    assert r.json()["push_status"] == {}
    assert client.post("/chat/approve", json={"key": "bad"}).status_code == 403

    assert client.post("/chat/clear", json={"key": "s3cret"}).json() == {"ok": True}
    assert cleared == [True]

    s = client.get("/chat/state", params={"key": "s3cret"})
    assert s.json()["goals"] == "g"
    assert client.get("/chat/state", params={"key": "no"}).status_code == 403


def test_chat_push_day(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    seen = {}
    monkeypatch.setattr(
        app_mod.coach, "push_day",
        lambda date: seen.update(date=date) or {"summary": f"Pushed {date}",
                                                "draft": [], "push_status": {date: "pushed"}},
    )
    r = client.post("/chat/push-day", json={"key": "s3cret", "date": "2026-07-14"})
    assert r.status_code == 200
    assert r.json()["summary"] == "Pushed 2026-07-14"
    assert r.json()["push_status"] == {"2026-07-14": "pushed"}
    assert client.post("/chat/push-day", json={"key": "bad", "date": "x"}).status_code == 403


def test_cron_nightly_requires_vercel_bearer(monkeypatch):
    """Vercel Cron authenticates with `Authorization: Bearer $CRON_SECRET`. An
    unauthenticated endpoint would let anyone burn LLM spend and rewrite the plan."""
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    import jim.jobs.nightly as nightly_mod

    ran = []
    monkeypatch.setattr(
        nightly_mod, "run_nightly",
        lambda: ran.append(True) or {"for_date": "2026-07-13", "elapsed_sec": 12.3},
    )

    assert client.get("/api/cron/nightly").status_code == 403
    assert client.get(
        "/api/cron/nightly", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403
    assert ran == []  # neither attempt executed the job

    r = client.get("/api/cron/nightly", headers={"Authorization": "Bearer cr0n"})
    assert r.status_code == 200
    assert r.json()["elapsed_sec"] == 12.3
    assert ran == [True]


def test_cron_nightly_shut_when_no_secret_configured(monkeypatch):
    """No CRON_SECRET => endpoint stays closed, rather than defaulting to open."""
    monkeypatch.setattr(
        app_mod, "settings",
        lambda: SimpleNamespace(chat_secret="s3cret", app_timezone="UTC", cron_secret=""),
    )
    assert client.get(
        "/api/cron/nightly", headers={"Authorization": "Bearer "}
    ).status_code == 403
