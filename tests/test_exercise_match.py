"""The semantic fallback: LLM only for what the string matcher can't place, its
answer validated against Garmin's real taxonomy, and the result cached.

Offline like everything else — the model call and the kv store are both faked."""

from datetime import date

import pytest

from jim.schemas import ExerciseStep, StructuredSession
from jim.tools import exercise_match
from jim.tools.exercise_match import CACHE_KEY, llm_match, semantic_resolver
from jim.tools.garmin import build_strength_payload, classify_all


@pytest.fixture
def fake_kv(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(exercise_match, "kv_get", lambda key: store.get(key))
    monkeypatch.setattr(exercise_match, "kv_set", lambda key, value: store.__setitem__(key, value))
    return store


@pytest.fixture
def fake_llm(monkeypatch):
    """Records what the model was asked; replies with whatever `answers` holds."""
    calls: list[list[str]] = []
    answers: dict = {}

    def llm(names, model=""):
        calls.append(list(names))
        return {n: answers[n] for n in names if n in answers}

    monkeypatch.setattr(exercise_match, "llm_match", llm)
    return calls, answers


def session(*names: str) -> StructuredSession:
    return StructuredSession(
        for_date=date(2026, 7, 15), kind="strength", title="Custom",
        est_duration_min=40,
        steps=[ExerciseStep(exercise=n, sets=3, reps=8) for n in names],
    )


def test_a_confident_match_never_reaches_the_model(fake_kv, fake_llm):
    calls, answers = fake_llm
    answers["Hip airplane"] = ("HIP_STABILITY", "HIP_CIRCLES")

    classified = classify_all(
        ["Goblet squat", "Romanian deadlift", "Hip airplane"], semantic_resolver()
    )

    assert calls == [["Hip airplane"]]  # the two it already knew cost nothing
    assert classified["Goblet squat"] == ("SQUAT", "GOBLET_SQUAT")
    assert classified["Hip airplane"] == ("HIP_STABILITY", "HIP_CIRCLES")


def test_a_lukewarm_match_is_second_guessed(fake_kv, fake_llm):
    """Sharing words with an exercise isn't being it: "Tibialis raise" matches
    PLATE_RAISES on words alone, and that's the wrong movement on the watch."""
    calls, answers = fake_llm
    answers["Tibialis raise"] = ("WARM_UP", "ANKLE_DORSIFLEXION_WITH_BAND")

    classified = classify_all(["Tibialis raise"], semantic_resolver())

    assert calls == [["Tibialis raise"]]
    assert classified["Tibialis raise"] == ("WARM_UP", "ANKLE_DORSIFLEXION_WITH_BAND")


def test_the_lexical_guess_stands_if_the_model_has_nothing_better(fake_kv, fake_llm):
    calls, _ = fake_llm  # model declines to answer
    classified = classify_all(["Monster walk"], semantic_resolver())
    assert calls == [["Monster walk"]]
    assert classified["Monster walk"] == ("RUN", "WALK")


def test_a_whole_session_is_one_call_not_one_per_exercise(fake_kv, fake_llm):
    calls, answers = fake_llm
    answers["Hip airplane"] = ("HIP_STABILITY", "HIP_CIRCLES")
    answers["Pallof press"] = ("CORE", "CABLE_CORE_PRESS")

    build_strength_payload(
        session("Goblet squat", "Hip airplane", "Pallof press"), resolver=semantic_resolver()
    )

    assert len(calls) == 1
    assert sorted(calls[0]) == ["Hip airplane", "Pallof press"]


def test_a_resolved_name_is_paid_for_once(fake_kv, fake_llm):
    calls, answers = fake_llm
    answers["Hip airplane"] = ("HIP_STABILITY", "HIP_CIRCLES")

    resolve = semantic_resolver()
    assert resolve(["Hip airplane"]) == {"Hip airplane": ("HIP_STABILITY", "HIP_CIRCLES")}
    # the same movement, spelled differently, is already paid for
    assert resolve(["hip  AIRPLANE"]) == {"hip  AIRPLANE": ("HIP_STABILITY", "HIP_CIRCLES")}

    assert len(calls) == 1
    assert fake_kv[CACHE_KEY] == {"hip airplane": ["HIP_STABILITY", "HIP_CIRCLES"]}


def test_a_movement_garmin_lacks_is_not_asked_about_twice(fake_kv, fake_llm):
    calls, _ = fake_llm  # the model answers nothing: Garmin has no equivalent

    resolve = semantic_resolver()
    assert resolve(["Faff about a bit"]) == {}
    assert resolve(["Faff about a bit"]) == {}

    assert len(calls) == 1  # the miss is cached too
    assert fake_kv[CACHE_KEY] == {"faff about a bit": None}


def test_an_unresolved_step_still_pushes_as_a_description(fake_kv, fake_llm):
    payload = build_strength_payload(session("Faff about a bit"), resolver=semantic_resolver())
    (group,) = payload["workoutSegments"][0]["workoutSteps"]
    (step,) = group["workoutSteps"]
    assert "category" not in step
    assert step["description"] == "Faff about a bit"


def test_payload_builders_are_offline_without_a_resolver(fake_llm):
    """No resolver injected -> no db, no LLM. This is how every other test runs."""
    calls, _ = fake_llm
    build_strength_payload(session("Goblet squat", "Hip airplane"))
    assert calls == []


# --- the model does not get to invent enums ----------------------------------


class FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


def fake_openai(monkeypatch, content: str):
    class FakeClient:
        def __init__(self, **_):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **_):
            return type("R", (), {"choices": [FakeChoice(content)]})()

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(
        "jim.config.settings", lambda: type("S", (), {"openrouter_api_key": "k"})()
    )


def test_an_invented_pair_is_discarded(monkeypatch):
    """CORE/SINGLE_LEG_CIRCLES doesn't exist — that exact pair was a real bug. A
    described step beats a wrong one, so a bad answer is dropped, not pushed."""
    fake_openai(monkeypatch, '{"Single-leg circles": {"category": "CORE",'
                             ' "exerciseName": "SINGLE_LEG_CIRCLES"}}')
    assert llm_match(["Single-leg circles"]) == {}


def test_an_exercise_from_the_wrong_category_is_discarded(monkeypatch):
    # GOBLET_SQUAT is real and SQUAT is real, but GOBLET_SQUAT is not in DEADLIFT
    fake_openai(monkeypatch, '{"Sissy squat": {"category": "DEADLIFT",'
                             ' "exerciseName": "GOBLET_SQUAT"}}')
    assert llm_match(["Sissy squat"]) == {}


def test_a_real_pair_survives(monkeypatch):
    fake_openai(monkeypatch, '{"Sissy squat": {"category": "SQUAT",'
                             ' "exerciseName": "GOBLET_SQUAT"}}')
    assert llm_match(["Sissy squat"]) == {"Sissy squat": ("SQUAT", "GOBLET_SQUAT")}


def test_a_category_only_answer_is_allowed(monkeypatch):
    fake_openai(monkeypatch, '{"Sissy squat": {"category": "SQUAT", "exerciseName": null}}')
    assert llm_match(["Sissy squat"]) == {"Sissy squat": ("SQUAT", None)}


def test_the_taxonomy_it_chooses_from_is_the_real_one():
    from jim.tools.exercise_match import taxonomy_prompt

    prompt = taxonomy_prompt()
    assert "SQUAT: " in prompt and "GOBLET_SQUAT" in prompt
    assert "CALF_RAISE: " in prompt
