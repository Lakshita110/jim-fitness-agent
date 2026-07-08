import json
from datetime import date

import pytest
from pydantic import ValidationError

from jim.agent.compose import build_user_prompt, parse_session
from jim.schemas import GarminToday, HistoryFeatures, NotionDay, ResearchHit

FOR_DATE = date(2026, 7, 7)

VALID = {
    "for_date": "2099-01-01",  # wrong on purpose — parser must override
    "kind": "strength",
    "title": "Upper pull",
    "steps": [{"exercise": "Barbell row", "sets": 3, "reps": 8, "weight_kg": 50}],
    "est_duration_min": 40,
    "rationale_summary": "pull day",
}


def test_parse_session_valid():
    session = parse_session(json.dumps(VALID), FOR_DATE)
    assert session.title == "Upper pull"
    assert session.steps[0].weight_kg == 50


def test_parse_session_overrides_model_date():
    assert parse_session(json.dumps(VALID), FOR_DATE).for_date == FOR_DATE


def test_parse_session_rejects_garbage():
    with pytest.raises(json.JSONDecodeError):
        parse_session("not json at all", FOR_DATE)
    with pytest.raises(ValidationError):
        parse_session(json.dumps({"kind": "party", "title": "x"}), FOR_DATE)


def test_user_prompt_includes_research_and_feedback():
    prompt = build_user_prompt(
        FOR_DATE,
        GarminToday(day=FOR_DATE),
        NotionDay(day=FOR_DATE),
        HistoryFeatures(as_of=FOR_DATE, window_days=28),
        [ResearchHit(source="pt-protocol", title="Isometrics", snippet="use holds")],
        revision_feedback=["forbidden exercise: 'Box jump'"],
    )
    assert "pt-protocol" in prompt
    assert "REJECTED" in prompt
    assert "Box jump" in prompt


def test_user_prompt_omits_empty_sections():
    prompt = build_user_prompt(
        FOR_DATE,
        GarminToday(day=FOR_DATE),
        NotionDay(day=FOR_DATE),
        HistoryFeatures(as_of=FOR_DATE, window_days=28),
        research=[],
    )
    assert "Research" not in prompt
    assert "REJECTED" not in prompt
