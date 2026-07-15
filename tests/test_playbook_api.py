"""/api/playbook (soft-baking-kettle plan Phase 4): the validated-JSON-textarea
editor. Arbitrary JSON from the client into Playbook.model_validate is new
attack surface, so the negative cases (malformed JSON, wrong shape, no partial
write) matter as much as the happy path."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import jim.app as app_mod
import jim.web.playbook_routes as playbook_routes
from jim import auth
from jim.auth import User
from jim.playbook import Playbook, WorkoutTemplate
from jim.web import deps

client = TestClient(app_mod.app)
TEST_USER = User(id=3, email="athlete@example.com")


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    monkeypatch.setattr(deps, "_ready", lambda: None)
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


class FakeStore:
    """In-memory stand-in for the playbooks table, scoped by user_id, so we can
    assert a rejected POST leaves the existing row untouched."""

    def __init__(self):
        self.by_user: dict[int, Playbook] = {}

    def load(self, user_id: int) -> Playbook:
        return self.by_user.get(user_id, Playbook())

    def save(self, user_id: int, pb: Playbook) -> None:
        self.by_user[user_id] = pb


def _wire_store(monkeypatch, store: FakeStore):
    monkeypatch.setattr(playbook_routes, "load_playbook", store.load)
    monkeypatch.setattr(playbook_routes, "save_playbook", store.save)


def test_get_playbook_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    assert client.get("/api/playbook").status_code == 403


def test_post_playbook_requires_auth(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    r = client.post("/api/playbook", json={"raw": "{}"})
    assert r.status_code == 403


def test_get_returns_seeded_playbook(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    store.by_user[TEST_USER.id] = Playbook(directives="No standing directives yet")
    _wire_store(monkeypatch, store)

    r = client.get("/api/playbook")
    assert r.status_code == 200
    assert r.json()["directives"] == "No standing directives yet"


def test_round_trips_a_valid_edit(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    _wire_store(monkeypatch, store)

    new_pb = {
        "rotation": ["a"],
        "workouts": {
            "a": {"key": "a", "label": "Day A", "sport": "strength_training", "blocks": []}
        },
        "pt_routines": {},
        "directives": "edited directives",
    }
    import json

    r = client.post("/api/playbook", json={"raw": json.dumps(new_pb)})
    assert r.status_code == 200 and r.json() == {"ok": True}

    r2 = client.get("/api/playbook")
    body = r2.json()
    assert body["directives"] == "edited directives"
    assert body["rotation"] == ["a"]
    assert body["workouts"]["a"]["label"] == "Day A"


def test_malformed_json_returns_400_not_500(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    _wire_store(monkeypatch, store)

    r = client.post("/api/playbook", json={"raw": "{not valid json"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_wrong_shape_workouts_as_list_returns_400_not_500(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    _wire_store(monkeypatch, store)

    import json

    bad = json.dumps({"rotation": [], "workouts": ["not", "a", "dict"], "directives": ""})
    r = client.post("/api/playbook", json={"raw": bad})
    assert r.status_code == 400
    assert "error" in r.json()


def test_workout_template_missing_required_field_returns_400_not_500(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    _wire_store(monkeypatch, store)

    import json

    # WorkoutTemplate requires `sport`; omit it.
    bad = json.dumps(
        {"rotation": [], "workouts": {"a": {"key": "a", "label": "Day A"}},
         "pt_routines": {}, "directives": ""}
    )
    r = client.post("/api/playbook", json={"raw": bad})
    assert r.status_code == 400
    assert "error" in r.json()


def test_rejected_post_does_not_partially_write(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", fake_settings)
    _sign_in(monkeypatch)
    store = FakeStore()
    original = Playbook(directives="original, unchanged")
    store.by_user[TEST_USER.id] = original
    _wire_store(monkeypatch, store)

    bad = '{"workouts": ["nope"]}'
    r = client.post("/api/playbook", json={"raw": bad})
    assert r.status_code == 400

    still = client.get("/api/playbook").json()
    assert still["directives"] == "original, unchanged"
    assert store.by_user[TEST_USER.id] is original


def test_playbook_model_validate_rejects_the_bad_shapes_directly():
    """Sanity-check the validation itself, independent of the route."""
    with pytest.raises(ValidationError):
        Playbook.model_validate({"workouts": ["not", "a", "dict"]})
    with pytest.raises(ValidationError):
        WorkoutTemplate.model_validate({"key": "a", "label": "x"})  # missing sport
