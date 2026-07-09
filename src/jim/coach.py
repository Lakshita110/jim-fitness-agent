"""Jim's coach chat: one conversation thread (single user) to iterate on a
plan for tomorrow or the week, keep long-term goals in plain language, and
push to Garmin only on explicit approve.

The model can LOOK THINGS UP mid-turn (bounded to MAX_TOOL_ROUNDS):
per-exercise performance history from the watch (checked before prescribing
weights), recent workout/adherence history, and research (curated corpus +
web). Lookups happen inside a turn and are not persisted to chat history.

State is deliberately simple — everything lives in the kv store:
- 'chat_history': last HISTORY_LIMIT messages [{role, content}]
- 'draft': the working plan, a list of StructuredSession dicts (dated days)
- 'goals': plain-text long-term goals block, rewritten by the model on request
- 'state': cached day snapshot (garmin/notion/features), refreshed hourly

Deps are injected (`CoachDeps`) so everything unit-tests without Postgres,
Garmin, Notion, or an LLM."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from jim.agent.validate import validate
from jim.config import (
    FORBIDDEN_EXERCISES,
    MAX_SESSION_MIN,
    MIN_DAYS_BETWEEN_LEG_SESSIONS,
    MODEL_FAST,
    OPENROUTER_BASE_URL,
)
from jim.schemas import HistoryFeatures, StructuredSession

log = logging.getLogger(__name__)

HISTORY_LIMIT = 30
STATE_TTL_MIN = 60
DRAFT_MAX_DAYS = 7
MAX_REPLY_CHARS = 3800
MAX_TOOL_ROUNDS = 4  # lookup rounds per turn — keeps cost bounded

# Lookups the model may call mid-conversation (OpenAI function schemas).
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "exercise_history",
            "description": "How the athlete actually performed a movement recently"
            " (per-session sets x reps @ weight, from watch data). ALWAYS check"
            " this before prescribing or changing a weight/rep target.",
            "parameters": {
                "type": "object",
                "properties": {"exercise": {"type": "string",
                                            "description": "movement name, e.g. 'goblet squat'"}},
                "required": ["exercise"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workout_history",
            "description": "Recent workouts and plan adherence over the last N days.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "lookback, default 14"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": "Search the curated rehab/training corpus and the web for"
            " grounded guidance. Use for pain-driven substitutions and cite sources.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are Jim, a careful strength & conditioning coach for one athlete
with knee and ankle constraints. You chat naturally and iterate on training plans.

Hard rules (never violate, even if asked):
- Never program: {forbidden}.
- Keep any session under {max_min} minutes.
- Leg sessions need at least {leg_gap} days since the last leg session.
- Respect pain and low readiness: prefer PT, mobility, or easy conditioning on bad days.

The PLAYBOOK below is the athlete's base A/B/C strength rotation and PT routines.
Prefer selecting a template unchanged: set its garmin_workout_id and template_key
on that day and leave steps empty (the existing Garmin workout is scheduled as-is,
weights preserved). Hand-build steps only when adapting.

LONG-TERM GOALS: the athlete's goals are a plain-text block you maintain. When they
state, change, or complete a goal, return the FULL rewritten goals block in "goals"
— that alone updates their memory; do not schedule anything for it. Weave active
goals into your planning (progressions, deloads, milestones).

DRAFT: the current working plan. Revise it when asked. A draft covers 1-{max_days}
dated days. Nothing touches the athlete's watch until they explicitly approve.

TOOLS: look things up instead of guessing. Call exercise_history BEFORE setting
any weight or rep target and progress conservatively from what was actually done
(+2.5-5% load or +1-2 reps after a solid session; hold or reduce after a rough
one). Call workout_history for what recently happened; call research for
pain-driven substitutions and cite the sources in your reply.

Today is {today}. Respond ONLY with a JSON object:
{{"reply": str,                      # plain text, no markdown — read as a chat message
  "draft": [session, ...] | null,   # null = keep current draft unchanged; [] = clear it
  "goals": str | null}}              # null = goals unchanged; string = replace block
Each session: {{"for_date": "YYYY-MM-DD", "kind": "strength|conditioning|mobility|rest",
  "title": str, "template_key": str|null, "garmin_workout_id": str|null,
  "steps": [{{"exercise": str, "sets": int, "reps": int|null, "duration_sec": int|null,
             "weight_kg": float|null, "notes": str}}],
  "est_duration_min": float, "rationale_summary": str}}"""


@dataclass
class CoachDeps:
    """Every side effect the coach needs, injectable for tests."""

    kv_get: Callable[[str], Any]
    kv_set: Callable[[str, Any], None]
    fetch_state: Callable[[], dict]  # fresh garmin/notion/features snapshot
    # (messages, tool_schemas|None) -> {"content": str|None, "tool_calls": [...]|None}
    llm: Callable[[list[dict], list[dict] | None], dict]
    lookup_tools: dict[str, Callable[..., str]]  # name -> callable, see TOOL_SCHEMAS
    schedule_workout: Callable[..., None]
    create_garmin_workout: Callable[..., Any]
    record_suggestion: Callable[..., int]
    playbook_text: Callable[[], str]
    now: Callable[[], datetime]

    @classmethod
    def live(cls) -> "CoachDeps":
        from zoneinfo import ZoneInfo

        from jim.config import settings
        from jim.db import kv_get, kv_set
        from jim.playbook import load_playbook
        from jim.tools import garmin, memory, notion
        from jim.tools.history import exercise_history, query_history, workout_history

        def now() -> datetime:
            return datetime.now(ZoneInfo(settings().app_timezone))

        def fetch_state() -> dict:
            today = now().date()
            return {
                "garmin": garmin.get_garmin_today(today).model_dump(mode="json"),
                "notion": notion.get_notion_logs(today).model_dump(mode="json"),
                "features": query_history(today).model_dump(mode="json"),
            }

        def llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
            from openai import OpenAI

            client = OpenAI(
                base_url=OPENROUTER_BASE_URL, api_key=settings().openrouter_api_key
            )
            # Always constrain content to JSON — a round that offers tools but
            # gets a text answer back (model declines to call one) must still
            # parse; response_format only governs the content field, not
            # whether the model may emit tool_calls instead.
            kwargs: dict = {
                "model": MODEL_FAST,
                "messages": messages,
                "response_format": {"type": "json_object"},
            }
            if tools:
                kwargs["tools"] = tools
            msg = client.chat.completions.create(**kwargs).choices[0].message
            return {
                "content": msg.content,
                "tool_calls": [
                    {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                    for c in (msg.tool_calls or [])
                ] or None,
            }

        def research(question: str) -> str:
            from jim.tools.research import research_training

            hits = research_training(question)
            return "\n".join(f"[{h.source}] {h.title}: {h.snippet}" for h in hits) or "(no hits)"

        return cls(
            kv_get=kv_get,
            kv_set=kv_set,
            fetch_state=fetch_state,
            llm=llm,
            lookup_tools={
                "exercise_history": exercise_history,
                "workout_history": workout_history,
                "research": research,
            },
            schedule_workout=garmin.schedule_workout,
            create_garmin_workout=garmin.create_garmin_workout,
            record_suggestion=memory.record_suggestion,
            playbook_text=lambda: load_playbook().to_prompt(),
            now=now,
        )


# --- state helpers ----------------------------------------------------------


def _cached_state(deps: CoachDeps) -> dict:
    cached = deps.kv_get("state") or {}
    fetched_at = cached.get("fetched_at")
    if fetched_at:
        age = deps.now() - datetime.fromisoformat(fetched_at)
        if age < timedelta(minutes=STATE_TTL_MIN):
            return cached["state"]
    try:
        state = deps.fetch_state()
    except Exception:
        log.exception("state snapshot failed; using stale/empty state")
        return cached.get("state", {})
    deps.kv_set("state", {"fetched_at": deps.now().isoformat(), "state": state})
    return state


def _features(state: dict, today: date) -> HistoryFeatures:
    raw = state.get("features")
    if raw:
        return HistoryFeatures.model_validate(raw)
    return HistoryFeatures(as_of=today, window_days=28)


def _parse_draft(raw: list, today: date) -> list[StructuredSession]:
    sessions = []
    for item in raw[:DRAFT_MAX_DAYS]:
        try:
            sessions.append(StructuredSession.model_validate(item))
        except Exception:
            log.warning("dropping unparseable draft day: %r", item)
    return sessions


def format_draft(sessions: list[StructuredSession]) -> str:
    """Human-readable draft summary (chat replies + approve confirmations)."""
    lines = []
    for s in sessions:
        head = f"{s.for_date} — {s.title} ({s.kind}, ~{s.est_duration_min:.0f} min)"
        if s.garmin_workout_id:
            head += f" [existing workout: {s.template_key or s.garmin_workout_id}]"
        lines.append(head)
        for step in s.steps[:10]:
            dose = f"{step.sets}x{step.reps}" if step.reps else f"{step.sets}x{step.duration_sec}s"
            weight = f" @ {step.weight_kg}kg" if step.weight_kg else ""
            lines.append(f"  • {step.exercise} — {dose}{weight}")
        if len(s.steps) > 10:
            lines.append(f"  … +{len(s.steps) - 10} more")
    return "\n".join(lines)


# --- the conversation -------------------------------------------------------


def _system_prompt(deps: CoachDeps, state: dict) -> str:
    today = deps.now().date()
    goals = deps.kv_get("goals") or "(no long-term goals recorded yet)"
    draft = deps.kv_get("draft") or []
    parts = [
        SYSTEM_PROMPT.format(
            forbidden=", ".join(FORBIDDEN_EXERCISES),
            max_min=MAX_SESSION_MIN,
            leg_gap=MIN_DAYS_BETWEEN_LEG_SESSIONS,
            max_days=DRAFT_MAX_DAYS,
            today=today.isoformat(),
        ),
        "# TODAY'S STATE\n" + json.dumps(state),
        "# LONG-TERM GOALS\n" + goals,
        "# CURRENT DRAFT\n" + (json.dumps(draft) if draft else "(empty)"),
        "# PLAYBOOK\n" + deps.playbook_text(),
    ]
    return "\n\n".join(parts)


def _loads_json(text: str) -> dict:
    """Lenient JSON parse — strips markdown fences some models emit."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned)


def _run_model(deps: CoachDeps, system: str, history: list[dict]) -> dict:
    """One model turn, with a bounded lookup loop: the model may call
    exercise_history / workout_history / research before answering."""
    msgs = [{"role": "system", "content": system}, *history]
    for _ in range(MAX_TOOL_ROUNDS):
        resp = deps.llm(msgs, TOOL_SCHEMAS)
        calls = resp.get("tool_calls")
        if not calls:
            return _loads_json(resp.get("content") or "")
        msgs.append({
            "role": "assistant",
            "content": resp.get("content"),
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls
            ],
        })
        for c in calls:
            try:
                fn = deps.lookup_tools[c["name"]]
                result = str(fn(**json.loads(c["arguments"] or "{}")))
            except Exception as e:  # a failed lookup shouldn't kill the turn
                log.warning("lookup %s failed: %s", c.get("name"), e)
                result = f"lookup failed: {e}"
            log.info("lookup %s(%s)", c["name"], c["arguments"])
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result[:4000]})
    # Lookup budget exhausted — force a final answer without tools.
    msgs.append({"role": "user", "content": "SYSTEM: answer now with the final JSON only."})
    resp = deps.llm(msgs, None)
    return _loads_json(resp.get("content") or "")


def converse(text: str, deps: CoachDeps | None = None) -> dict:
    """One chat turn. Returns {reply, draft} and persists history/draft/goals."""
    deps = deps or CoachDeps.live()
    today = deps.now().date()
    state = _cached_state(deps)
    history: list[dict] = deps.kv_get("chat_history") or []
    history = history[-HISTORY_LIMIT:] + [{"role": "user", "content": text}]

    system = _system_prompt(deps, state)
    try:
        out = _run_model(deps, system, history)
    except Exception:
        log.exception("coach turn failed")
        return {"reply": "I couldn't process that just now — try again in a moment.",
                "draft": deps.kv_get("draft") or []}

    reply = str(out.get("reply") or "")[:MAX_REPLY_CHARS]

    # Goals: a non-null string replaces the block (that IS the long-term memory).
    if isinstance(out.get("goals"), str):
        deps.kv_set("goals", out["goals"])

    # Draft: null keeps the current one; a list replaces it (after guardrail).
    if isinstance(out.get("draft"), list):
        sessions = _parse_draft(out["draft"], today)
        features = _features(state, today)
        violations = {
            s.for_date.isoformat(): result.violations
            for s in sessions
            if not (result := validate(s, features)).ok
        }
        if violations:
            history.append({"role": "assistant", "content": json.dumps(out)})
            history.append({
                "role": "user",
                "content": "SYSTEM: the validator rejected these days — fix and resend"
                " the full JSON: " + json.dumps(violations),
            })
            try:
                out = _run_model(deps, system, history)
                reply = str(out.get("reply") or reply)[:MAX_REPLY_CHARS]
                sessions = _parse_draft(out.get("draft") or [], today)
            except Exception:
                log.exception("revision turn failed")
            kept = [s for s in sessions if validate(s, _features(state, today)).ok]
            if len(kept) < len(sessions):
                reply += "\n(I dropped day(s) that still broke the safety rules.)"
            sessions = kept
        deps.kv_set("draft", [s.model_dump(mode="json") for s in sessions])

    history.append({"role": "assistant", "content": reply})
    deps.kv_set("chat_history", history[-HISTORY_LIMIT:])
    return {"reply": reply, "draft": deps.kv_get("draft") or []}


def approve(deps: CoachDeps | None = None) -> str:
    """Push every day in the draft to Garmin; record suggestions; clear draft."""
    deps = deps or CoachDeps.live()
    draft = _parse_draft(deps.kv_get("draft") or [], deps.now().date())
    if not draft:
        return "Nothing to push — the draft is empty."
    pushed = []
    for session in draft:
        if session.kind == "rest":
            pushed.append(f"{session.for_date}: rest day (nothing scheduled)")
        elif session.garmin_workout_id:
            deps.schedule_workout(session.garmin_workout_id, session.for_date)
            pushed.append(f"{session.for_date}: scheduled {session.title}")
        else:
            ref = deps.create_garmin_workout(session)
            deps.schedule_workout(ref.workout_id, session.for_date)
            pushed.append(f"{session.for_date}: created + scheduled {session.title}")
        deps.record_suggestion(
            session.for_date, session, session.rationale_summary,
            False, "fast", source="chat",
        )
    deps.kv_set("draft", [])
    summary = "Pushed to Garmin:\n" + "\n".join(pushed)
    history = (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:]
    history.append({"role": "assistant", "content": summary})
    deps.kv_set("chat_history", history)
    return summary


def clear(deps: CoachDeps | None = None) -> None:
    """Start a fresh conversation (draft and goals survive)."""
    deps = deps or CoachDeps.live()
    deps.kv_set("chat_history", [])


def current_state(deps: CoachDeps | None = None) -> dict:
    """What the UI shows on load: recent messages + working draft + goals."""
    deps = deps or CoachDeps.live()
    return {
        "history": (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:],
        "draft": deps.kv_get("draft") or [],
        "goals": deps.kv_get("goals") or "",
    }
