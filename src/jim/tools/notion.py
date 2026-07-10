"""Notion tools — READ ONLY. Notion is a data source (habits/knee log,
tasks); all interaction happens in Jim's chat.

Wired to the real workspace schemas (resolves PLAN.md §12 Q1):

- "habits db" (knee+habit log): title `name`, date `date`, number `pain level`,
  multi-select `knee pain` (mixes severity words none/mild/moderate/severe with
  locations left/right/ankles/hips/quads/shins), select `pain location`, text
  `pain notes`, checkbox `physical therapy`, habit checkboxes (cardio, reading,
  strength training, vitamins, dental care), formula `day score`.
- "tasks ": title `task`, dates `do date` / `due date`, status `status`.

`pain level` is often left blank; when it is, a numeric level is derived from
the severity word in `knee pain` (none=0, mild=2, moderate=5, severe=8) so the
off-heuristic always has a number to work with."""

import logging
from datetime import date, timedelta
from typing import Any

from jim.config import settings
from jim.schemas import NotionDay

log = logging.getLogger(__name__)

# habits db property names
PROP_DATE = "date"
PROP_PAIN_LEVEL = "pain level"
PROP_KNEE_PAIN = "knee pain"
PROP_PAIN_LOCATION = "pain location"
PROP_PAIN_NOTES = "pain notes"
PROP_PT = "physical therapy"
PROP_DAY_SCORE = "day score"

# tasks db property names
PROP_TASK_TITLE = "task"
PROP_DO_DATE = "do date"
PROP_DUE_DATE = "due date"
PROP_STATUS = "status"

SEVERITY_TO_LEVEL = {"none": 0, "mild": 2, "moderate": 5, "severe": 8}
SEVERITY_WORDS = frozenset(SEVERITY_TO_LEVEL)

_client: Any = None
_data_source_ids: dict[str, str] = {}


def client() -> Any:
    global _client
    if _client is None:
        from notion_client import Client

        _client = Client(auth=settings().notion_token)
    return _client


def _data_source_id(database_id: str) -> str:
    """Notion's API (2025-09+) queries data sources, not databases directly.
    Most databases still have exactly one data source; resolve + cache it."""
    if database_id not in _data_source_ids:
        db = client().databases.retrieve(database_id=database_id)
        sources = db.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"database {database_id} has no data sources")
        _data_source_ids[database_id] = sources[0]["id"]
    return _data_source_ids[database_id]


def _query(database_id: str, **kwargs: Any) -> dict[str, Any]:
    return client().data_sources.query(data_source_id=_data_source_id(database_id), **kwargs)


# --- property extraction helpers -------------------------------------------


def _prop(page: dict[str, Any], name: str) -> dict[str, Any]:
    return page.get("properties", {}).get(name, {})


def _number(page: dict[str, Any], name: str) -> float | None:
    prop = _prop(page, name)
    if prop.get("type") == "formula":
        return (prop.get("formula") or {}).get("number")
    return prop.get("number")


def _checkbox(page: dict[str, Any], name: str) -> bool:
    return bool(_prop(page, name).get("checkbox"))


def _multi_select(page: dict[str, Any], name: str) -> list[str]:
    return [opt.get("name", "") for opt in _prop(page, name).get("multi_select") or []]


def _text(page: dict[str, Any], name: str) -> str:
    prop = _prop(page, name)
    rich = prop.get("rich_text") or prop.get("title") or []
    if rich:
        return "".join(part.get("plain_text", "") for part in rich)
    select = prop.get("select")
    return select.get("name", "") if select else ""


def parse_knee_log_page(page: dict[str, Any], day: date) -> NotionDay:
    knee_pain = _multi_select(page, PROP_KNEE_PAIN)
    severities = [w for w in knee_pain if w in SEVERITY_WORDS]
    locations = [w for w in knee_pain if w not in SEVERITY_WORDS]

    pain = _number(page, PROP_PAIN_LEVEL)
    if pain is None and severities:
        pain = max(SEVERITY_TO_LEVEL[w] for w in severities)

    habits = {
        name: bool(prop.get("checkbox"))
        for name, prop in page.get("properties", {}).items()
        if prop.get("type") == "checkbox" and name != PROP_PT
    }
    score = _number(page, PROP_DAY_SCORE)
    return NotionDay(
        day=day,
        pain_level=int(pain) if pain is not None else None,
        pain_location=", ".join(locations) or _text(page, PROP_PAIN_LOCATION),
        pain_notes=_text(page, PROP_PAIN_NOTES),
        pt_done=_checkbox(page, PROP_PT),
        habits=habits,
        day_score=int(score) if score is not None else None,
    )


def parse_task_page(page: dict[str, Any]) -> str:
    return _text(page, PROP_TASK_TITLE)


# --- tool contracts (PLAN.md §7) -------------------------------------------


def get_notion_logs(day: date) -> NotionDay:
    """Pain level/location, PT adherence, habits, and tomorrow's planned tasks."""
    cfg = settings()

    log_rows = _query(
        cfg.notion_knee_log_db_id,
        filter={"property": PROP_DATE, "date": {"equals": day.isoformat()}},
        page_size=1,
    ).get("results", [])
    result = (
        parse_knee_log_page(log_rows[0], day) if log_rows else NotionDay(day=day)
    )

    tomorrow = (day + timedelta(days=1)).isoformat()
    # The tasks DB is optional scheduling context; if it isn't shared with the
    # integration, degrade to no tasks rather than blanking the whole read.
    try:
        task_rows = _query(
            cfg.notion_tasks_db_id,
            filter={
                "and": [
                    {
                        "or": [
                            {"property": PROP_DO_DATE, "date": {"equals": tomorrow}},
                            {"property": PROP_DUE_DATE, "date": {"equals": tomorrow}},
                        ]
                    },
                    {"property": PROP_STATUS, "status": {"does_not_equal": "Done"}},
                ]
            },
            page_size=20,
        ).get("results", [])
        result.tomorrow_tasks = [t for t in (parse_task_page(p) for p in task_rows) if t]
    except Exception:
        log.warning("notion tasks db unavailable; continuing without task context", exc_info=True)
    return result
