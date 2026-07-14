"""Notion property-mapping tests against a recorded fixture of the real
"habits db" schema — no live API."""

import json
from datetime import date
from pathlib import Path

import jim.tools.notion as notion
from jim.tools.notion import parse_knee_log_page

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


def test_fractional_day_score_is_not_truncated():
    """`day score` is a Notion formula returning a fraction — int() used to
    silently flatten every partial day (0.5) to 0."""
    page = {
        "properties": {
            "day score": {"type": "formula", "formula": {"type": "number", "number": 0.5}}
        }
    }
    assert parse_knee_log_page(page, DAY).day_score == 0.5


def test_missing_day_score_stays_none():
    assert parse_knee_log_page({"properties": {}}, DAY).day_score is None


# --- range backfill --------------------------------------------------------


def _page(day: str, note: str) -> dict:
    return {
        "properties": {
            "date": {"type": "date", "date": {"start": day}},
            "pain notes": {"type": "rich_text",
                           "rich_text": [{"plain_text": note}]},
        }
    }


def test_page_date_reads_the_date_property():
    assert notion.page_date(_page("2026-07-06", "x")) == DAY
    assert notion.page_date({"properties": {}}) is None
    assert notion.page_date({"properties": {"date": {"date": None}}}) is None


def test_get_notion_logs_range_follows_pagination(monkeypatch):
    """Notion pages at 100 results; a 90-day window can exceed that, and stopping
    at page one would silently drop the oldest half of the history."""
    pages = [
        {"results": [_page("2026-07-06", "wrists")], "has_more": True, "next_cursor": "c1"},
        {"results": [_page("2026-07-04", "driving")], "has_more": False, "next_cursor": None},
    ]
    seen = []

    def fake_query(user_id, db_id, **kwargs):
        seen.append(kwargs.get("start_cursor"))
        return pages[len(seen) - 1]

    monkeypatch.setattr(notion, "_query", fake_query)
    monkeypatch.setattr(notion, "_knee_log_db_id", lambda user_id: "db")

    days = notion.get_notion_logs_range(1, date(2026, 7, 1), DAY)
    assert [d.day for d in days] == [date(2026, 7, 6), date(2026, 7, 4)]
    assert [d.pain_notes for d in days] == ["wrists", "driving"]
    assert seen == [None, "c1"]  # second call carried the cursor


def test_get_notion_logs_range_skips_pages_with_no_date(monkeypatch):
    """An undated page can't be attributed to a day — drop it rather than
    guessing, which would corrupt the pain history."""
    monkeypatch.setattr(
        notion, "_query",
        lambda user_id, db_id, **kw: {"results": [_page("2026-07-06", "ok"),
                                                  {"properties": {}}], "has_more": False},
    )
    monkeypatch.setattr(notion, "_knee_log_db_id", lambda user_id: "db")
    days = notion.get_notion_logs_range(1, date(2026, 7, 1), DAY)
    assert [d.day for d in days] == [DAY]
