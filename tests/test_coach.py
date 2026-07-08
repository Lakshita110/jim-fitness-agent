"""Coach chat tests with fully injected deps — no LLM, Postgres, or APIs."""

import json
from datetime import date, datetime

from jim.coach import CoachDeps, approve, clear, converse, current_state, format_draft
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
        self.created: list = []
        self.recorded: list = []
        self.lookups = lookups or {}

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
            fetch_state=lambda: {"features": {"as_of": TODAY.isoformat(),
                                              "window_days": 28,
                                              "weekly_volume_min": 200,
                                              "days_since_legs": 3}},
            llm=llm,
            lookup_tools=self.lookups,
            schedule_workout=lambda wid, on: self.scheduled.append((wid, on)),
            create_garmin_workout=create,
            record_suggestion=record,
            playbook_text=lambda: "PLAYBOOK-BLOCK",
            now=lambda: NOW,
        )


def test_goal_only_turn_stores_goals_and_no_draft():
    f = Fakes([{"reply": "Got it — 5k by spring.", "draft": None,
                "goals": "Run a 5k by spring; knee health first."}])
    out = converse("long term I want to run a 5k by spring", f.deps())
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
    converse("hey", f.deps())
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
    out = converse("plan thu+fri", f.deps())
    assert len(out["draft"]) == 2
    assert f.kv["draft"][1]["garmin_workout_id"] == "1625297181"


def test_invalid_day_triggers_revision_then_drops_if_still_bad():
    bad = day("2026-07-09", steps=[{"exercise": "Box jump", "sets": 3, "reps": 5,
                                    "duration_sec": None, "weight_kg": None, "notes": ""}])
    f = Fakes([
        {"reply": "plan", "draft": [bad], "goals": None},
        {"reply": "revised", "draft": [bad], "goals": None},  # still invalid
    ])
    out = converse("plan tomorrow", f.deps())
    assert len(f.llm_calls) == 2  # one revision attempt
    assert out["draft"] == []  # still-invalid day dropped
    assert "safety rules" in out["reply"]


def test_draft_capped_at_seven_days():
    f = Fakes([{"reply": "two weeks!", "goals": None,
                "draft": [day(f"2026-07-{9 + i:02d}") for i in range(10)]}])
    out = converse("plan two weeks", f.deps())
    assert len(out["draft"]) == 7


def test_null_draft_keeps_current_one():
    f = Fakes([{"reply": "sounds good", "draft": None, "goals": None}])
    f.kv["draft"] = [day("2026-07-09")]
    out = converse("thanks", f.deps())
    assert len(out["draft"]) == 1


def test_approve_schedules_by_id_and_creates_custom_days():
    f = Fakes()
    f.kv["draft"] = [
        day("2026-07-09", garmin_workout_id="1414012813", template_key="full_body_a",
            title="Full Body A", steps=[]),
        day("2026-07-10", title="Custom upper"),
        day("2026-07-11", kind="rest", title="Rest", steps=[]),
    ]
    summary = approve(f.deps())
    # template day scheduled directly; custom day created then scheduled
    assert ("1414012813", date(2026, 7, 9)) in f.scheduled
    assert ("9999", date(2026, 7, 10)) in f.scheduled
    assert len(f.created) == 1
    # all three recorded as chat-sourced (rest day too — it blocks the nightly run)
    assert [r[2] for r in f.recorded] == ["chat", "chat", "chat"]
    assert f.kv["draft"] == []  # cleared after push
    assert "Full Body A" in summary and "rest day" in summary


def test_approve_with_empty_draft_is_a_noop():
    f = Fakes()
    assert "empty" in approve(f.deps())
    assert f.scheduled == [] and f.recorded == []


def test_llm_failure_degrades_gracefully():
    f = Fakes()
    deps = f.deps()
    object.__setattr__(deps, "llm", lambda m: (_ for _ in ()).throw(RuntimeError("down")))
    out = converse("hello", deps)
    assert "try again" in out["reply"]


def test_clear_resets_history_but_keeps_draft_and_goals():
    f = Fakes()
    f.kv["chat_history"] = [{"role": "user", "content": "old"}]
    f.kv["draft"] = [day("2026-07-09")]
    f.kv["goals"] = "keep me"
    clear(f.deps())
    state = current_state(f.deps())
    assert state["history"] == []
    assert len(state["draft"]) == 1
    assert state["goals"] == "keep me"


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
    out = converse("bump my goblet squat", f.deps())
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
    out = converse("what does the evidence say", f.deps())
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
    out = converse("hi", f.deps())
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
