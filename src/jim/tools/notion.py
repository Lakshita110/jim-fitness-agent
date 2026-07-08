"""Notion tools: read the habits/knee log and tasks, write proposals.

Wired to the real workspace schemas (resolves PLAN.md §12 Q1/Q3):

- "habits db" (knee+habit log): title `name`, date `date`, number `pain level`,
  multi-select `knee pain` (mixes severity words none/mild/moderate/severe with
  locations left/right/ankles/hips/quads/shins), select `pain location`, text
  `pain notes`, checkbox `physical therapy`, habit checkboxes (cardio, reading,
  strength training, vitamins, dental care), formula `day score`.
- "tasks ": title `task`, dates `do date` / `due date`, status `status`.
- "training proposals": title `name`, date `date`, selects `kind` + `status`,
  checkbox `research used`. Proposals land as `status=proposed` for morning
  review.

`pain level` is often left blank; when it is, a numeric level is derived from
the severity word in `knee pain` (none=0, mild=2, moderate=5, severe=8) so the
off-heuristic always has a number to work with."""

import logging
from datetime import date, datetime, timedelta
from typing import Any

from jim.config import settings
from jim.schemas import CheckIn, NotionDay, StructuredSession

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


def client() -> Any:
    global _client
    if _client is None:
        from notion_client import Client

        _client = Client(auth=settings().notion_token)
    return _client


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


def parse_checkin_page(page: dict[str, Any], day: date) -> CheckIn:
    minutes = _number(page, "minutes")
    edited_raw = page.get("last_edited_time")
    return CheckIn(
        for_date=day,
        note=_text(page, "note"),
        focus=_text(page, "focus"),
        location=_text(page, "location"),
        minutes=int(minutes) if minutes is not None else None,
        energy=_text(page, "energy"),
        edited_ts=(
            datetime.fromisoformat(edited_raw.replace("Z", "+00:00"))
            if edited_raw
            else None
        ),
    )


def get_checkin(day: date) -> CheckIn:
    """The athlete's own input for `day` (empty CheckIn if none was written)."""
    cfg = settings()
    rows = client().databases.query(
        database_id=cfg.notion_checkin_db_id,
        filter={"property": "date", "date": {"equals": day.isoformat()}},
        page_size=1,
    ).get("results", [])
    return parse_checkin_page(rows[0], day) if rows else CheckIn(for_date=day)


# --- tool contracts (PLAN.md §7) -------------------------------------------


def get_notion_logs(day: date) -> NotionDay:
    """Pain level/location, PT adherence, habits, and tomorrow's planned tasks."""
    cfg = settings()
    api = client()

    log_rows = api.databases.query(
        database_id=cfg.notion_knee_log_db_id,
        filter={"property": PROP_DATE, "date": {"equals": day.isoformat()}},
        page_size=1,
    ).get("results", [])
    result = (
        parse_knee_log_page(log_rows[0], day) if log_rows else NotionDay(day=day)
    )

    tomorrow = (day + timedelta(days=1)).isoformat()
    task_rows = api.databases.query(
        database_id=cfg.notion_tasks_db_id,
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
    return result


def write_notion(
    for_date: date,
    plan: StructuredSession,
    rationale: str,
    research_used: bool = False,
) -> None:
    """Write the proposal + reasoning to the training proposals DB (status=proposed)."""
    cfg = settings()
    steps_text = "\n".join(
        f"- {s.exercise}: {s.sets}x{s.reps or ''}"
        + (f" @ {s.weight_kg}kg" if s.weight_kg else "")
        + (f" ({s.duration_sec}s)" if s.duration_sec else "")
        for s in plan.steps
    )
    client().pages.create(
        parent={"database_id": cfg.notion_proposal_db_id},
        properties={
            "name": {"title": [{"text": {"content": f"{for_date}: {plan.title}"}}]},
            "date": {"date": {"start": for_date.isoformat()}},
            "kind": {"select": {"name": plan.kind}},
            "status": {"select": {"name": "proposed"}},
            "research used": {"checkbox": research_used},
        },
        children=[
            _paragraph(f"Est. duration: {plan.est_duration_min:.0f} min"),
            _paragraph(steps_text or "(rest day — no steps)"),
            _paragraph(f"Why: {rationale}"),
        ],
    )
    log.info("wrote proposal for %s to Notion", for_date)


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }
