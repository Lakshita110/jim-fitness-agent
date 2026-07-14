"""Coach chat tests with fully injected deps — no LLM, Postgres, or APIs."""

import json
from datetime import date, datetime

from jim.coach import (
    CoachDeps,
    _session_from_calendar_item,
    _sig,
    _sync_calendar_into_draft,
    approve,
    clear,
    converse,
    current_state,
    format_draft,
    format_duration,
    push_day,
)
from jim.playbook import Playbook, WorkoutTemplate
from jim.schemas import ExerciseStep, StructuredSession, WorkoutRef

NOW = datetime(2026, 7, 8, 19, 0)
TODAY = NOW.date()


def day(iso: str, **overrides) -> dict:
    base = {
        "for_date": iso,
        "kind": "strength",
        "title": "Upper push",
        "template_key": None,
        "garmin_workout_id": None,
        "steps": [{"exercise": "Bench press", "sets": 3, "reps": 8,
                   "duration_sec": None, "weight_kg": 40, "notes": ""}],
        "est_duration_min": 45,
        "rationale_summary": "push day",
    }
    base.update(overrides)
    return base


class Fakes:
    def __init__(self, llm_outputs=None, lookups=None):
        self.kv: dict = {}
        self.llm_outputs = llm_outputs or []
        self.llm_calls: list[list[dict]] = []
        self.scheduled: list = []
        self.cleared: list = []
        self.created: list = []
        self.recorded: list = []
        self.lookups = lookups or {}
        self.state: dict = {"features": {"as_of": TODAY.isoformat(),
                                         "window_days": 28,
                                         "weekly_volume_min": 200,
                                         "days_since_legs": 3}}

    def deps(self) -> CoachDeps:
        def llm(messages, tools=None):
            self.llm_calls.append(messages)
            out = self.llm_outputs[min(len(self.llm_calls) - 1, len(self.llm_outputs) - 1)]
            if isinstance(out, dict) and out.get("tool_calls"):
                return out  # raw tool-call response
            return {"content": json.dumps(out), "tool_calls": None}

        def create(session):
            self.created.append(session)
            return WorkoutRef(workout_id="9999")

        def record(for_date, plan, rationale, research_used, tier, source="nightly"):
            self.recorded.append((for_date, plan.title, source))
            return len(self.recorded)

        return CoachDeps(
            kv_get=self.kv.get,
            kv_set=self.kv.__setitem__,
            fetch_state=lambda: self.state,
            llm=llm,
            lookup_tools=self.lookups,
            schedule_workout=lambda wid, on: self.scheduled.append((wid, on)),
            clear_schedule=lambda on: self.cleared.append(on),
            create_garmin_workout=create,
            record_suggestion=record,
            playbook_text=lambda: "PLAYBOOK-BLOCK",
            now=lambda: NOW,
        )


def test_goal_only_turn_stores_goals_and_no_draft():
    f = Fakes([{"reply": "Got it — 5k by spring.", "draft": None,
                "goals": "Run a 5k by spring; knee health first."}])
    out = converse("long term I want to run a 5k by spring", 1, f.deps())
    assert f.kv["goals"] == "Run a 5k by spring; knee health first."
    assert out["draft"] == []  # nothing scheduled, nothing drafted
    assert "5k" in out["reply"]
    # history persisted with both turns
    roles = [m["role"] for m in f.kv["chat_history"]]
    assert roles == ["user", "assistant"]


def test_context_includes_playbook_goals_state_and_draft():
    f = Fakes([{"reply": "ok", "draft": None, "goals": None}])
    f.kv["goals"] = "GOALS-BLOCK"
    f.kv["draft"] = [day("2026-07-09")]
    converse("hey", 1, f.deps())
    system = f.llm_calls[0][0]["content"]
    assert "PLAYBOOK-BLOCK" in system
    assert "GOALS-BLOCK" in system
    assert "Upper push" in system  # current draft is in context
    assert TODAY.isoformat() in system


def test_valid_draft_is_stored_and_returned():
    f = Fakes([{"reply": "here's the week", "goals": None,
                "draft": [day("2026-07-09"),
                          day("2026-07-10", kind="mobility", title="PT Day · Home",
                              garmin_workout_id="1625297181", template_key="pt_home",
                              steps=[])]}])
    out = converse("plan thu+fri", 1, f.deps())
    assert len(out["draft"]) == 2
    assert f.kv["draft"][1]["garmin_workout_id"] == "1625297181"


def test_invalid_day_triggers_revision_then_drops_if_still_bad():
    bad = day("2026-07-09", steps=[{"exercise": "Box jump", "sets": 3, "reps": 5,
                                    "duration_sec": None, "weight_kg": None, "notes": ""}])
    f = Fakes([
        {"reply": "plan", "draft": [bad], "goals": None},
        {"reply": "revised", "draft": [bad], "goals": None},  # still invalid
    ])
    out = converse("plan tomorrow", 1, f.deps())
    assert len(f.llm_calls) == 2  # one revision attempt
    assert out["draft"] == []  # still-invalid day dropped
    # the reply names the date and the reason, rather than a vague "some day(s)"
    assert "2026-07-09" in out["reply"]
    assert "forbidden exercise" in out["reply"]


def test_draft_capped_at_seven_days():
    # Short days, so the weekly volume budget isn't the binding constraint here
    # — this is isolating the DRAFT_MAX_DAYS truncation.
    f = Fakes([{"reply": "two weeks!", "goals": None,
                "draft": [day(f"2026-07-{9 + i:02d}", est_duration_min=20)
                          for i in range(10)]}])
    out = converse("plan two weeks", 1, f.deps())
    assert len(out["draft"]) == 7


def test_weekly_budget_is_spent_across_the_plan_not_per_day():
    """The athlete's bug: a full Mon-Fri plan couldn't be built because each day
    was tested against the whole week's headroom. A week that fits the budget in
    total must now survive intact."""
    f = Fakes([{"reply": "here's your week", "goals": None,
                "draft": [day(f"2026-07-{13 + i:02d}", est_duration_min=45)
                          for i in range(5)]}])
    # trailing 400 min -> budget 400*1.1+30 = 470; the plan totals 225
    f.state["features"] = {"as_of": "2026-07-12", "window_days": 28,
                           "weekly_volume_min": 400, "days_since_legs": None}
    out = converse("plan monday to friday", 1, f.deps())
    assert len(f.llm_calls) == 1          # no revision needed
    assert len(out["draft"]) == 5         # every day survived
    assert "Dropped" not in out["reply"]


def test_null_draft_keeps_current_one():
    f = Fakes([{"reply": "sounds good", "draft": None, "goals": None}])
    f.kv["draft"] = [day("2026-07-09")]
    out = converse("thanks", 1, f.deps())
    assert len(out["draft"]) == 1


def test_approve_schedules_by_id_and_creates_custom_days():
    f = Fakes()
    f.kv["draft"] = [
        day("2026-07-09", garmin_workout_id="1414012813", template_key="full_body_a",
            title="Full Body A", steps=[]),
        day("2026-07-10", title="Custom upper"),
        day("2026-07-11", kind="rest", title="Rest", steps=[]),
    ]
    summary = approve(1, f.deps())
    # template day scheduled directly; custom day created then scheduled
    assert ("1414012813", date(2026, 7, 9)) in f.scheduled
    assert ("9999", date(2026, 7, 10)) in f.scheduled
    assert len(f.created) == 1
    # all three recorded as chat-sourced (rest day too — it blocks the nightly run)
    assert [r[2] for r in f.recorded] == ["chat", "chat", "chat"]
    # draft is kept (now visible as on-watch), and the two real days are tracked
    assert len(f.kv["draft"]) == 3
    assert set(f.kv["pushed"]) == {"2026-07-09", "2026-07-10"}  # rest day not on watch
    assert "Full Body A" in summary and "rest day" in summary


def test_adapted_template_day_pushes_the_edits_not_the_stock_workout():
    """Regression: the model echoes full_body_a's Garmin ID alongside the swapped
    exercises, and the push scheduled stock Full Body A — the athlete's custom
    plan never reached the watch."""
    f = Fakes()
    f.kv["draft"] = [
        day("2026-07-09", title="Full Body A (knee-friendly)",
            garmin_workout_id="1414012813", template_key="full_body_a",
            steps=[{"exercise": "Goblet squat", "sets": 3, "reps": 12,
                    "duration_sec": None, "weight_kg": 16, "notes": ""},
                   {"exercise": "Leg press", "sets": 3, "reps": 10,
                    "duration_sec": None, "weight_kg": 40, "notes": ""}]),
    ]
    summary = approve(1, f.deps())
    # a NEW workout is built from the steps; the template ID is never scheduled
    assert f.created and f.created[0].steps[1].exercise == "Leg press"
    assert f.scheduled == [("9999", date(2026, 7, 9))]
    assert ("1414012813", date(2026, 7, 9)) not in f.scheduled
    assert "created + scheduled" in summary


def test_untouched_template_day_still_schedules_by_id():
    """The other half: an unchanged template must keep its loaded Garmin weights."""
    f = Fakes()
    f.kv["draft"] = [day("2026-07-09", title="Full Body A", steps=[],
                         garmin_workout_id="1414012813", template_key="full_body_a")]
    approve(1, f.deps())
    assert f.scheduled == [("1414012813", date(2026, 7, 9))]
    assert f.created == []


def test_partial_edit_merges_and_keeps_other_days():
    f = Fakes([{"reply": "made Fri easier", "goals": None,
                "draft": [day("2026-07-10", title="Easy mobility", kind="mobility",
                              steps=[], garmin_workout_id="1625297181")]}])
    f.kv["draft"] = [day("2026-07-09", title="Full Body A"), day("2026-07-10")]
    out = converse("make friday easier", 1, f.deps(), scope_date="2026-07-10")
    # Thu untouched, Fri replaced — the week isn't wiped by a one-day edit
    assert [d["for_date"] for d in out["draft"]] == ["2026-07-09", "2026-07-10"]
    assert out["draft"][0]["title"] == "Full Body A"
    assert out["draft"][1]["title"] == "Easy mobility"
    # scope hint reached the model
    assert "editing ONLY 2026-07-10" in f.llm_calls[0][0]["content"]


def test_empty_draft_list_wipes_plan():
    f = Fakes([{"reply": "cleared", "draft": [], "goals": None}])
    f.kv["draft"] = [day("2026-07-09")]
    out = converse("scrap the plan", 1, f.deps())
    assert out["draft"] == []


def test_push_day_pushes_one_and_tracks_status():
    f = Fakes()
    f.kv["draft"] = [day("2026-07-09", title="Full Body A"), day("2026-07-10")]
    res = push_day("2026-07-10", 1, f.deps())
    # only the one day was scheduled; the other is left alone
    assert f.scheduled == [("9999", date(2026, 7, 10))]
    assert res["push_status"] == {"2026-07-10": "pushed"}
    assert "Pushed to Garmin" in res["summary"]
    assert f.cleared == []  # first push, nothing to unschedule


def test_push_day_update_unschedules_then_reschedules():
    f = Fakes()
    f.kv["draft"] = [day("2026-07-10")]
    push_day("2026-07-10", 1, f.deps())
    push_day("2026-07-10", 1, f.deps())  # push again = update
    assert f.cleared == [date(2026, 7, 10)]  # old schedule cleared before re-push
    assert len(f.scheduled) == 2


def test_push_status_flags_day_edited_after_push():
    f = Fakes()
    f.kv["draft"] = [day("2026-07-10", title="Full Body A")]
    push_day("2026-07-10", 1, f.deps())
    # edit that day after pushing
    f.kv["draft"] = [day("2026-07-10", title="Full Body A",
                         steps=[{"exercise": "Bench press", "sets": 3, "reps": 10,
                                 "duration_sec": None, "weight_kg": 45, "notes": ""}])]
    state = current_state(1, f.deps())
    assert state["push_status"] == {"2026-07-10": "modified"}


def test_approve_with_empty_draft_is_a_noop():
    f = Fakes()
    assert "empty" in approve(1, f.deps())
    assert f.scheduled == [] and f.recorded == []


def test_llm_failure_degrades_gracefully():
    f = Fakes()
    deps = f.deps()
    object.__setattr__(deps, "llm", lambda m: (_ for _ in ()).throw(RuntimeError("down")))
    out = converse("hello", 1, deps)
    assert "try again" in out["reply"]


def test_clear_resets_history_but_keeps_draft_and_goals():
    f = Fakes()
    f.kv["chat_history"] = [{"role": "user", "content": "old"}]
    f.kv["draft"] = [day("2026-07-09")]
    f.kv["goals"] = "keep me"
    clear(1, f.deps())
    state = current_state(1, f.deps())
    assert state["history"] == []
    assert len(state["draft"]) == 1
    assert state["goals"] == "keep me"
    # new stat-card keys are present and null-safe when Garmin/Notion are absent
    assert state["readiness"] is None
    assert state["pain"] is None


def test_lookup_tool_round_feeds_result_back():
    # Turn 1: model asks for exercise history. Turn 2: final answer using it.
    f = Fakes(
        llm_outputs=[
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "exercise_history",
                 "arguments": json.dumps({"exercise": "goblet squat"})},
            ]},
            {"reply": "Last time you did 3x12 @ 16kg — let's go 18kg.",
             "draft": [day("2026-07-09", steps=[{"exercise": "Goblet squat", "sets": 3,
                                                 "reps": 10, "duration_sec": None,
                                                 "weight_kg": 18, "notes": ""}])],
             "goals": None},
        ],
        lookups={"exercise_history": lambda exercise: f"{exercise}: 2026-07-01: 3x12 @ 16kg"},
    )
    out = converse("bump my goblet squat", 1, f.deps())
    assert "18kg" in out["reply"]
    assert out["draft"][0]["steps"][0]["weight_kg"] == 18
    # second LLM call saw the tool result
    tool_msgs = [m for m in f.llm_calls[1] if m.get("role") == "tool"]
    assert tool_msgs and "3x12 @ 16kg" in tool_msgs[0]["content"]


def test_failed_lookup_does_not_kill_the_turn():
    f = Fakes(
        llm_outputs=[
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "research", "arguments": json.dumps({"question": "x"})},
            ]},
            {"reply": "ok without research", "draft": None, "goals": None},
        ],
        lookups={"research": lambda question: (_ for _ in ()).throw(RuntimeError("no key"))},
    )
    out = converse("what does the evidence say", 1, f.deps())
    assert out["reply"] == "ok without research"
    tool_msgs = [m for m in f.llm_calls[1] if m.get("role") == "tool"]
    assert "lookup failed" in tool_msgs[0]["content"]


def test_tool_budget_forces_final_answer():
    ask_again = {"content": None, "tool_calls": [
        {"id": "c1", "name": "workout_history", "arguments": "{}"},
    ]}
    f = Fakes(
        llm_outputs=[ask_again, ask_again, ask_again, ask_again,
                     {"reply": "final", "draft": None, "goals": None}],
        lookups={"workout_history": lambda days=14: "stuff"},
    )
    out = converse("hi", 1, f.deps())
    assert out["reply"] == "final"
    # 4 tool rounds + 1 forced final = 5 llm calls
    assert len(f.llm_calls) == 5
    assert f.llm_calls[-1][-1]["content"].startswith("SYSTEM: answer now")


def test_format_draft_renders_doses_and_template_refs():
    sessions = [
        StructuredSession(
            for_date=date(2026, 7, 9), kind="strength", title="Custom upper",
            steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8, weight_kg=40)],
            est_duration_min=45,
        ),
        StructuredSession(
            for_date=date(2026, 7, 10), kind="mobility", title="PT Day · Home",
            garmin_workout_id="1625297181", template_key="pt_home", est_duration_min=33,
        ),
    ]
    text = format_draft(sessions)
    assert "Bench press — 3x8 @ 40.0kg" in text
    assert "[existing workout: pt_home]" in text


def test_format_duration_prefers_minutes_over_long_second_counts():
    assert format_duration(30) == "30s"    # a short hold stays in seconds
    assert format_duration(45) == "45s"
    assert format_duration(60) == "1m"
    assert format_duration(600) == "10m"
    assert format_duration(1800) == "30m"  # not "1800s"
    assert format_duration(90) == "2m"     # rounded, not "1.5m"
    assert format_duration(None) == "0s"


def test_system_prompt_carries_balance_guidance_not_a_volume_budget():
    """Balance is advice, so it only works if it reaches the model as context."""
    f = Fakes([{"reply": "ok", "draft": None, "goals": None}])
    converse("plan my week", 1, f.deps())
    system = f.llm_calls[0][0]["content"]
    assert "NO weekly minute budget" in system
    assert "legs, push, pull, core and conditioning" in system


def test_system_prompt_shows_the_current_draft_skew():
    f = Fakes([{"reply": "ok", "draft": None, "goals": None}])
    f.kv["draft"] = [day(f"2026-07-{9 + i:02d}", title="Push") for i in range(3)]
    converse("how's my week look", 1, f.deps())
    system = f.llm_calls[0][0]["content"]
    assert "Current draft: push 100%" in system
    assert "nothing for legs" in system


# --- calendar sync ------------------------------------------------------------


def _pb() -> Playbook:
    return Playbook(
        rotation=["a"],
        workouts={
            "a": WorkoutTemplate(
                key="a", label="Full Body A", garmin_workout_id="555",
                sport="strength_training",
            ),
        },
    )


def _calendar_deps(kv: dict, kv_set_calls: list | None = None) -> CoachDeps:
    def kv_set(key, value):
        if kv_set_calls is not None:
            kv_set_calls.append(key)
        kv[key] = value

    return CoachDeps(
        kv_get=kv.get,
        kv_set=kv_set,
        fetch_state=lambda: {},
        llm=lambda *a, **k: {},
        lookup_tools={},
        schedule_workout=lambda *a, **k: None,
        clear_schedule=lambda *a, **k: None,
        create_garmin_workout=lambda s: None,
        record_suggestion=lambda *a, **kw: 1,
        playbook_text=lambda: "",
        now=lambda: NOW,
        playbook=_pb,
    )


def test_session_from_calendar_item_prefers_calendar_title_over_template_label():
    item = {"date": date(2026, 7, 15), "workout_id": "555",
            "title": "Full Body A (modified)"}
    s = _session_from_calendar_item(item, _pb())
    assert s.template_key == "a"
    assert s.kind == "strength"  # inferred from the matched template's sport
    assert s.title == "Full Body A (modified)"  # not the template's "Full Body A"
    assert s.garmin_workout_id == "555"
    assert s.steps == []  # template pick contract: no steps, schedule by ID


def test_session_from_calendar_item_falls_back_when_no_template_matches():
    item = {"date": date(2026, 7, 16), "workout_id": "999", "title": "One-off ride"}
    s = _session_from_calendar_item(item, _pb())
    assert s.template_key is None
    assert s.kind == "strength"  # default, not guessed harder than that
    assert s.title == "One-off ride"
    assert s.garmin_workout_id == "999"


def test_sync_never_overwrites_an_existing_draft_day():
    """The load-bearing guarantee: Jim's (or the athlete's) own plan always wins
    over a calendar read, even when the calendar disagrees."""
    kv = {"draft": [day("2026-07-15", title="Jim's plan")]}
    deps = _calendar_deps(kv)
    calendar_items = [
        {"date": date(2026, 7, 15), "workout_id": "999", "title": "Garmin disagrees"},
        {"date": date(2026, 7, 16), "workout_id": "555", "title": "Full Body A"},
    ]
    _sync_calendar_into_draft(deps, calendar_items)

    draft = kv["draft"]
    assert len(draft) == 2
    d15 = next(d for d in draft if d["for_date"] == "2026-07-15")
    assert d15["title"] == "Jim's plan"  # untouched

    d16 = next(d for d in draft if d["for_date"] == "2026-07-16")
    assert d16["garmin_workout_id"] == "555"
    assert d16["steps"] == []
    assert d16["template_key"] == "a"

    pushed = kv["pushed"]
    assert pushed["2026-07-16"]["sig"] == _sig(StructuredSession.model_validate(d16))
    assert "2026-07-15" not in pushed  # only the newly-added day is marked pushed


def test_sync_adds_nothing_and_skips_kv_set_when_calendar_is_fully_covered():
    kv = {"draft": [day("2026-07-15")]}
    kv_set_calls: list = []
    deps = _calendar_deps(kv, kv_set_calls)
    _sync_calendar_into_draft(
        deps, [{"date": date(2026, 7, 15), "workout_id": "1", "title": "already planned"}]
    )
    assert kv_set_calls == []
    assert kv["draft"] == [day("2026-07-15")]  # unchanged


def test_sync_from_empty_draft_adds_all_calendar_days():
    kv: dict = {}
    deps = _calendar_deps(kv)
    _sync_calendar_into_draft(deps, [
        {"date": date(2026, 7, 15), "workout_id": "555", "title": "Full Body A"},
        {"date": date(2026, 7, 16), "workout_id": "999", "title": "One-off"},
    ])
    assert len(kv["draft"]) == 2
    assert set(kv["pushed"]) == {"2026-07-15", "2026-07-16"}


# --- fetch_state / CoachDeps.live: calendar as an independent source ---------


def test_calendar_source_failure_does_not_blank_other_sources(monkeypatch):
    import jim.db as db_mod
    import jim.tools.garmin as garmin_mod
    import jim.tools.history as history_mod
    import jim.tools.notion as notion_mod
    from jim.schemas import GarminToday, HistoryFeatures, NotionDay, ReadinessRead

    store: dict = {}
    monkeypatch.setattr(db_mod, "kv_get", lambda user_id, key: store.get(key))
    monkeypatch.setattr(db_mod, "kv_set", lambda user_id, key, value: store.__setitem__(key, value))
    monkeypatch.setattr(garmin_mod, "get_garmin_today", lambda user_id, day: GarminToday(day=day))

    def boom(*a, **kw):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(garmin_mod, "get_scheduled_workouts", boom)
    monkeypatch.setattr(notion_mod, "get_notion_logs", lambda user_id, day: NotionDay(day=day))
    monkeypatch.setattr(history_mod, "query_history",
                         lambda user_id, day: HistoryFeatures(as_of=day, window_days=28))
    monkeypatch.setattr(history_mod, "readiness_read",
                         lambda user_id, day: ReadinessRead(as_of=day))

    deps = CoachDeps.live(1)
    state = deps.fetch_state()
    assert "calendar" not in state  # the only source that failed
    assert "garmin" in state and "notion" in state
    assert "features" in state and "readiness" in state


def test_calendar_source_populates_state_with_iso_dates(monkeypatch):
    import jim.db as db_mod
    import jim.tools.garmin as garmin_mod
    import jim.tools.history as history_mod
    import jim.tools.notion as notion_mod
    from jim.schemas import GarminToday, HistoryFeatures, NotionDay, ReadinessRead

    store: dict = {}
    monkeypatch.setattr(db_mod, "kv_get", lambda user_id, key: store.get(key))
    monkeypatch.setattr(db_mod, "kv_set", lambda user_id, key, value: store.__setitem__(key, value))
    monkeypatch.setattr(garmin_mod, "get_garmin_today", lambda user_id, day: GarminToday(day=day))
    monkeypatch.setattr(
        garmin_mod, "get_scheduled_workouts",
        lambda user_id, start, end: [{"date": start, "workout_id": "1", "title": "Lift"}],
    )
    monkeypatch.setattr(notion_mod, "get_notion_logs", lambda user_id, day: NotionDay(day=day))
    monkeypatch.setattr(history_mod, "query_history",
                         lambda user_id, day: HistoryFeatures(as_of=day, window_days=28))
    monkeypatch.setattr(history_mod, "readiness_read",
                         lambda user_id, day: ReadinessRead(as_of=day))

    deps = CoachDeps.live(1)
    state = deps.fetch_state()
    assert state["calendar"] == [
        {"date": state["calendar"][0]["date"], "workout_id": "1", "title": "Lift"}
    ]
    assert isinstance(state["calendar"][0]["date"], str)  # JSON-safe, not a raw date object


def test_cached_state_reuses_calendar_within_the_ttl(monkeypatch):
    """Calendar sync rides the existing 1-hour cache — one Garmin call per hour
    of chat activity, not one per turn."""
    import jim.db as db_mod
    import jim.tools.garmin as garmin_mod
    import jim.tools.history as history_mod
    import jim.tools.notion as notion_mod
    from jim.coach import _cached_state
    from jim.schemas import GarminToday, HistoryFeatures, NotionDay, ReadinessRead

    store: dict = {}
    monkeypatch.setattr(db_mod, "kv_get", lambda user_id, key: store.get(key))
    monkeypatch.setattr(db_mod, "kv_set", lambda user_id, key, value: store.__setitem__(key, value))
    monkeypatch.setattr(garmin_mod, "get_garmin_today", lambda user_id, day: GarminToday(day=day))
    calls = []

    def scheduled(user_id, start, end):
        calls.append((start, end))
        return []

    monkeypatch.setattr(garmin_mod, "get_scheduled_workouts", scheduled)
    monkeypatch.setattr(notion_mod, "get_notion_logs", lambda user_id, day: NotionDay(day=day))
    monkeypatch.setattr(history_mod, "query_history",
                         lambda user_id, day: HistoryFeatures(as_of=day, window_days=28))
    monkeypatch.setattr(history_mod, "readiness_read",
                         lambda user_id, day: ReadinessRead(as_of=day))

    deps = CoachDeps.live(1)
    deps.now = lambda: NOW  # CoachDeps is a plain (non-frozen) dataclass
    _cached_state(deps)
    _cached_state(deps)
    assert len(calls) == 1
