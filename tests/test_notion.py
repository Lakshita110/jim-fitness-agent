"""Notion property-mapping tests against a recorded fixture of the real
"habits db" schema — no live API."""

import json
from datetime import date
from pathlib import Path

from vesper.tools.notion import parse_knee_log_page, parse_task_page

DAY = date(2026, 7, 6)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "knee_log_page.json").read_text())


def test_parse_knee_log_page():
    parsed = parse_knee_log_page(FIXTURE, DAY)
    # `pain level` number is blank; "mild" in the knee-pain multi-select maps to 2
    assert parsed.pain_level == 2
    assert parsed.pain_location == "right, ankles"
    assert parsed.pain_notes == "sore after stairs"
    assert parsed.pt_done is True
    assert parsed.day_score == 7
    assert parsed.habits == {
        "strength training": True,
        "cardio": False,
        "reading": True,
        "vitamins": False,
        "dental care": False,
    }  # physical therapy excluded — tracked as pt_done


def test_explicit_pain_level_wins_over_severity_word():
    page = json.loads(json.dumps(FIXTURE))
    page["properties"]["pain level"]["number"] = 6
    assert parse_knee_log_page(page, DAY).pain_level == 6


def test_severity_only_maps_each_word():
    for word, level in [("none", 0), ("mild", 2), ("moderate", 5), ("severe", 8)]:
        page = {
            "properties": {
                "knee pain": {"type": "multi_select", "multi_select": [{"name": word}]}
            }
        }
        assert parse_knee_log_page(page, DAY).pain_level == level


def test_parse_knee_log_page_tolerates_missing_properties():
    parsed = parse_knee_log_page({"properties": {}}, DAY)
    assert parsed.pain_level is None
    assert parsed.pt_done is False
    assert parsed.habits == {}
    assert parsed.pain_location == ""


def test_parse_task_page():
    page = {
        "properties": {
            "task": {"type": "title", "title": [{"plain_text": "PT appointment"}]}
        }
    }
    assert parse_task_page(page) == "PT appointment"
    assert parse_task_page({"properties": {}}) == ""
