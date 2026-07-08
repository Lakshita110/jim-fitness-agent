from types import SimpleNamespace

from fastapi.testclient import TestClient

import jim.app as app_mod

client = TestClient(app_mod.app)


def fake_settings():
    return SimpleNamespace(chat_secret="s3cret", app_timezone="America/New_York")


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
    monkeypatch.setattr(app_mod, "handle_chat_message", lambda text, now: f"echo: {text}")
    r = client.post("/chat/message", json={"key": "s3cret", "text": "knee sore"})
    assert r.status_code == 200
    assert r.json() == {"reply": "echo: knee sore"}
    assert client.post("/chat/message", json={"key": "bad", "text": "x"}).status_code == 403
    assert client.post("/chat/message", json={"key": "s3cret", "text": "  "}).status_code == 400
