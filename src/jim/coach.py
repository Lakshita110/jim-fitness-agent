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

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from jim.agent.validate import balance_notes, plan_balance, validate_plan
from jim.config import (
    BALANCE_MAX_SHARE,
    FORBIDDEN_EXERCISES,
    MAX_SESSION_MIN,
    MIN_DAYS_BETWEEN_LEG_SESSIONS,
    MODEL_FAST,
    OPENROUTER_BASE_URL,
)
from jim.playbook import Playbook, load_playbook, use_existing_workout
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

VOICE: warm, playful, a little flirty — the hype-you-up gym partner who teases
gently, throws the occasional wink, and genuinely celebrates wins ("look at you
go 💪"). Charm is welcome; emojis sparingly (at most one). But never let the fun
bury the substance — the plan, the numbers, and the safety calls stay crisp and
come first. Read the room: if they're in pain, wiped out, or having a rough day,
drop the banter and just be kind and solid.

Hard rules (never violate, even if asked):
- Never program: {forbidden}.
- Keep any session under {max_min} minutes.
- Leg sessions need at least {leg_gap} days since the last leg session.
- Respect pain and low readiness: prefer PT, mobility, or easy conditioning on bad days.

PAIN: the athlete's own log is the source of truth — "pain_level" (0-10),
"pain_location", and "pain_notes". Read the notes, not just the number:
"recent_pain_notes" in the features is their recent history, newest first. If
the same complaint keeps recurring, name it and work around that joint rather
than re-prescribing what keeps aggravating it. ("day_score" is habit tracking —
it says nothing about pain or training; ignore it when planning.)

LOAD & READINESS: TODAY'S STATE includes a "readiness" read (acute:chronic
workload ratio + recovery). Let its "status" steer intensity — push = room to
build, steady = hold load, ease = keep it light, rest = PT/mobility/off. Don't
add volume or load when the ratio is already high or recovery is poor; say one
plain line about why when it changes the plan.

The PLAYBOOK below is the athlete's base A/B/C strength rotation and PT routines.
A day is EITHER a template pick OR an adaptation — never both:
- Template unchanged: set garmin_workout_id and template_key, and leave "steps"
  EMPTY. The existing Garmin workout is scheduled as-is, weights preserved.
- Adapted (you swapped, added, dropped, or re-loaded ANYTHING): set
  garmin_workout_id AND template_key to null and list the FULL steps. Never
  return a template's garmin_workout_id next to steps you changed — the steps
  are what the athlete sees, so the steps are what gets built on the watch.

LONG-TERM GOALS: the athlete's goals are a plain-text block you maintain. When they
state, change, or complete a goal, return the FULL rewritten goals block in "goals"
— that alone updates their memory; do not schedule anything for it. Weave active
goals into your planning (progressions, deloads, milestones).

DRAFT: the current working plan, 1-{max_days} dated days. Revise it when asked.
Return in "draft" ONLY the day(s) you are adding or changing — each is merged by
for_date onto the existing plan, so days you omit are kept untouched. To plan a
whole week, return all its days. To cancel a day, return it as kind "rest"; to
fill a rest/empty day, just return a real session for that date (subject to the
hard rules — a rest day is often rest for a reason, so say so if it's unwise).
Return "draft": [] only to wipe the entire plan. Nothing touches the athlete's
watch until they explicitly approve.

TOOLS: look things up instead of guessing. Call exercise_history BEFORE setting
any weight or rep target and progress conservatively from what was actually done
(+2.5-5% load or +1-2 reps after a solid session; hold or reduce after a rough
one). Call workout_history for what recently happened; call research for
pain-driven substitutions and cite the sources in your reply.

Today is {today}. Respond ONLY with a JSON object:
{{"reply": str,                      # chat message; light markdown ok (**bold**, "- " bullets,
                                     # "## " headings) for anything structured like a weekly
                                     # schedule — it renders. Don't reach for it in ordinary
                                     # back-and-forth replies.
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
    clear_schedule: Callable[..., None]  # unschedule planned workouts on a date
    create_garmin_workout: Callable[..., Any]
    record_suggestion: Callable[..., int]
    playbook_text: Callable[[], str]
    now: Callable[[], datetime]
    # Only consulted when a day carries a template ID, to tell an untouched
    # template apart from an adapted one before pushing.
    playbook: Callable[[], Playbook] = load_playbook

    @classmethod
    def live(cls) -> "CoachDeps":
        from zoneinfo import ZoneInfo

        from jim.config import settings
        from jim.db import kv_get, kv_set
        from jim.playbook import load_playbook
        from jim.tools import garmin, memory, notion
        from jim.tools.history import (
            exercise_history,
            query_history,
            readiness_read,
            workout_history,
        )

        def now() -> datetime:
            return datetime.now(ZoneInfo(settings().app_timezone))

        def fetch_state() -> dict:
            today = now().date()
            # Each source degrades independently — a down integration (e.g. an
            # unshared Notion) must not blank Garmin, features, or readiness.
            sources = {
                "garmin": lambda: garmin.get_garmin_today(today),
                "notion": lambda: notion.get_notion_logs(today),
                "features": lambda: query_history(today),
                "readiness": lambda: readiness_read(today),
            }
            state: dict = {}
            for name, fetch in sources.items():
                try:
                    state[name] = fetch().model_dump(mode="json")
                except Exception:
                    log.warning("state source %r unavailable this turn", name, exc_info=True)
            return state

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
            clear_schedule=garmin.clear_schedule,
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


def format_duration(secs: int | None) -> str:
    """Short holds read naturally in seconds (a 30s plank); a minute or more
    reads better in minutes (1800s -> 30m)."""
    if not secs:
        return "0s"
    if secs < 60:
        return f"{secs}s"
    mins = secs / 60
    return f"{mins:g}m" if mins.is_integer() else f"{round(mins)}m"


def format_draft(sessions: list[StructuredSession]) -> str:
    """Human-readable draft summary (chat replies + approve confirmations)."""
    lines = []
    for s in sessions:
        head = f"{s.for_date} — {s.title} ({s.kind}, ~{s.est_duration_min:.0f} min)"
        # Only claim "existing workout" when that's what will actually be pushed:
        # a day carrying steps is built from those steps, template ID or not.
        if s.garmin_workout_id and not s.steps:
            head += f" [existing workout: {s.template_key or s.garmin_workout_id}]"
        lines.append(head)
        for step in s.steps[:10]:
            dose = (f"{step.sets}x{step.reps}" if step.reps
                    else f"{step.sets}x{format_duration(step.duration_sec)}")
            weight = f" @ {step.weight_kg}kg" if step.weight_kg else ""
            lines.append(f"  • {step.exercise} — {dose}{weight}")
        if len(s.steps) > 10:
            lines.append(f"  … +{len(s.steps) - 10} more")
    return "\n".join(lines)


# --- push tracking ----------------------------------------------------------
# 'pushed' kv: {for_date_iso: {"title", "sig", "pushed_at"}} — what is on the
# watch. `sig` is a content hash so the UI can flag a day edited since its push.


def _sig(session: StructuredSession) -> str:
    raw = json.dumps(session.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _push_status(deps: CoachDeps, sessions: list[StructuredSession]) -> dict[str, str]:
    """Per-date badge state: 'pushed' (on watch, unchanged) or 'modified'
    (edited since its push, needs a re-push)."""
    pushed = deps.kv_get("pushed") or {}
    status: dict[str, str] = {}
    for s in sessions:
        fd = s.for_date.isoformat()
        if fd in pushed:
            status[fd] = "pushed" if pushed[fd].get("sig") == _sig(s) else "modified"
    return status


def _push_one(deps: CoachDeps, session: StructuredSession) -> str:
    """Schedule a single session on the watch and record it. Returns a summary line.

    An untouched template is scheduled by ID (its loaded weights live on Garmin,
    not here); anything the athlete adapted is built fresh from its steps. The
    steps are what they can see in the plan, so the steps are what must land on
    the watch — see playbook.use_existing_workout."""
    fd = session.for_date
    as_template = bool(
        session.kind != "rest"
        and session.garmin_workout_id  # short-circuits: no template ID, no playbook read
        and use_existing_workout(session, deps.playbook())
    )
    if session.kind == "rest":
        pass  # rest schedules nothing on the watch
    elif as_template:
        deps.schedule_workout(session.garmin_workout_id, fd)
    else:
        ref = deps.create_garmin_workout(session)
        deps.schedule_workout(ref.workout_id, fd)
    deps.record_suggestion(
        fd, session, session.rationale_summary, False, "fast", source="chat",
    )
    if session.kind == "rest":
        return f"{fd}: rest day (nothing scheduled)"
    verb = "scheduled" if as_template else "created + scheduled"
    return f"{fd}: {verb} {session.title}"


def _mark_pushed(deps: CoachDeps, session: StructuredSession) -> None:
    pushed = deps.kv_get("pushed") or {}
    fd = session.for_date.isoformat()
    if session.kind == "rest":
        pushed.pop(fd, None)  # a rest day leaves nothing on the watch
    else:
        pushed[fd] = {"title": session.title, "sig": _sig(session),
                      "pushed_at": deps.now().isoformat()}
    deps.kv_set("pushed", pushed)


# --- the conversation -------------------------------------------------------


def _system_prompt(deps: CoachDeps, state: dict) -> str:
    today = deps.now().date()
    goals = deps.kv_get("goals") or "(no long-term goals recorded yet)"
    draft = deps.kv_get("draft") or []
    # Balance is advice, not a hard rule — so it has to reach the model as
    # context. Show it the current draft's split and what's skewed about it.
    sessions = _parse_draft(draft, today)
    balance = plan_balance(sessions)
    notes = balance_notes(sessions)
    balance_block = "# BALANCE\nSpread the loading work evenly across legs, push," \
        " pull, core and conditioning — no single one should own more than" \
        f" {BALANCE_MAX_SHARE:.0%} of the plan. Mobility/PT sits outside this and" \
        " can run daily. There is NO weekly minute budget: keep each day under" \
        f" {MAX_SESSION_MIN} min and plan as many days as the athlete asks for.\n"
    if balance:
        balance_block += "Current draft: " + ", ".join(
            f"{g} {s:.0%}" for g, s in sorted(balance.items(), key=lambda x: -x[1])
        ) + "\n"
    if notes:
        balance_block += "Skew to fix: " + "; ".join(notes)

    parts = [
        SYSTEM_PROMPT.format(
            forbidden=", ".join(FORBIDDEN_EXERCISES),
            max_min=MAX_SESSION_MIN,
            leg_gap=MIN_DAYS_BETWEEN_LEG_SESSIONS,
            max_days=DRAFT_MAX_DAYS,
            today=today.isoformat(),
        ),
        balance_block,
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


def converse(text: str, deps: CoachDeps | None = None,
             scope_date: str | None = None) -> dict:
    """One chat turn. Returns {reply, draft, push_status} and persists
    history/draft/goals. `scope_date` (an ISO date) narrows the edit to a single
    day — the model is told to return only that day, merged onto the plan."""
    deps = deps or CoachDeps.live()
    today = deps.now().date()
    state = _cached_state(deps)
    history: list[dict] = deps.kv_get("chat_history") or []
    history = history[-HISTORY_LIMIT:] + [{"role": "user", "content": text}]

    system = _system_prompt(deps, state)
    if scope_date:
        system += (
            f"\n\n# EDIT SCOPE\nThe athlete is editing ONLY {scope_date}. Return"
            f' just that one day in "draft" (for_date {scope_date}); do not include'
            " or change any other day."
        )
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

    # Draft: null keeps the current one; [] wipes it; a non-empty list is merged
    # by for_date onto the current plan (so single-day edits can't drop others).
    if isinstance(out.get("draft"), list):
        if not out["draft"]:
            deps.kv_set("draft", [])
        else:
            features = _features(state, today)
            existing = _parse_draft(deps.kv_get("draft") or [], today)

            def merge(new: list[StructuredSession]) -> list[StructuredSession]:
                by_date = {s.for_date.isoformat(): s for s in existing}
                for s in new:
                    by_date[s.for_date.isoformat()] = s
                return [by_date[k] for k in sorted(by_date)][:DRAFT_MAX_DAYS]

            # Validate the merged plan — that's what gets saved, and leg spacing
            # only means anything when the days are seen together.
            plan = merge(_parse_draft(out["draft"], today))
            violations = validate_plan(plan, features)
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
                    plan = merge(_parse_draft(out.get("draft") or [], today))
                except Exception:
                    log.exception("revision turn failed")
                violations = validate_plan(plan, features)
                if violations:
                    plan = [s for s in plan if s.for_date.isoformat() not in violations]
                    reply += "\n(Dropped " + ", ".join(
                        f"{d} — {v[0]}" for d, v in sorted(violations.items())
                    ) + ")"

            deps.kv_set("draft", [s.model_dump(mode="json") for s in plan])

    history.append({"role": "assistant", "content": reply})
    deps.kv_set("chat_history", history[-HISTORY_LIMIT:])
    saved = _parse_draft(deps.kv_get("draft") or [], today)
    return {"reply": reply, "draft": deps.kv_get("draft") or [],
            "push_status": _push_status(deps, saved), "today": today.isoformat()}


def approve(deps: CoachDeps | None = None) -> str:
    """Push every day in the draft to Garmin and record suggestions. The draft
    is kept (each day now shows as on-watch) so it stays visible and editable;
    already-pushed days are re-scheduled cleanly (unschedule first)."""
    deps = deps or CoachDeps.live()
    draft = _parse_draft(deps.kv_get("draft") or [], deps.now().date())
    if not draft:
        return "Nothing to push — the draft is empty."
    pushed_before = deps.kv_get("pushed") or {}
    lines = []
    for session in draft:
        fd = session.for_date.isoformat()
        if fd in pushed_before and session.kind != "rest":
            deps.clear_schedule(session.for_date)  # replace, don't duplicate
        lines.append(_push_one(deps, session))
        _mark_pushed(deps, session)
    summary = "Pushed to Garmin:\n" + "\n".join(lines)
    history = (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:]
    history.append({"role": "assistant", "content": summary})
    deps.kv_set("chat_history", history)
    return summary


def push_day(for_date: str, deps: CoachDeps | None = None) -> dict:
    """Push (or update) a single draft day to Garmin. Returns
    {summary, draft, push_status}. Re-pushing an already-pushed day unschedules
    the prior one first so the watch never ends up with a duplicate."""
    deps = deps or CoachDeps.live()
    today = deps.now().date()
    draft = _parse_draft(deps.kv_get("draft") or [], today)
    draft_json = [s.model_dump(mode="json") for s in draft]
    try:
        target = date.fromisoformat(for_date)
    except ValueError:
        return {"summary": "That date didn't look right.", "draft": draft_json,
                "push_status": _push_status(deps, draft)}
    session = next((s for s in draft if s.for_date == target), None)
    if session is None:
        return {"summary": f"{for_date} isn't in the current plan.",
                "draft": draft_json, "push_status": _push_status(deps, draft)}

    updating = for_date in (deps.kv_get("pushed") or {})
    if updating and session.kind != "rest":
        deps.clear_schedule(target)  # replace, don't duplicate
    if session.kind == "rest":
        if updating:
            deps.clear_schedule(target)
        deps.record_suggestion(
            target, session, session.rationale_summary, False, "fast", source="chat",
        )
        _mark_pushed(deps, session)  # drops it from the pushed map
        summary = f"Cleared {for_date} — rest day, nothing left on the watch."
    else:
        line = _push_one(deps, session)
        _mark_pushed(deps, session)
        verb = "Updated on Garmin" if updating else "Pushed to Garmin"
        summary = f"{verb} — {line.split(': ', 1)[-1]}"
    return {"summary": summary, "draft": draft_json,
            "push_status": _push_status(deps, draft)}


def clear(deps: CoachDeps | None = None) -> None:
    """Start a fresh conversation (draft and goals survive)."""
    deps = deps or CoachDeps.live()
    deps.kv_set("chat_history", [])


def current_state(deps: CoachDeps | None = None) -> dict:
    """What the UI shows on load: recent messages + working draft + goals,
    plus the readiness verdict and latest pain read for the stat cards."""
    deps = deps or CoachDeps.live()
    readiness = None
    pain = None
    try:  # a state hiccup must never break the page load
        state = _cached_state(deps)
        readiness = state.get("readiness")
        notion = state.get("notion") or {}
        if (
            notion.get("pain_level") is not None
            or notion.get("pain_location")
            or notion.get("pain_notes")
        ):
            pain = {
                "level": notion.get("pain_level"),
                "location": notion.get("pain_location") or "",
                "notes": notion.get("pain_notes") or "",
                "day": notion.get("day"),
            }
    except Exception:
        log.exception("state read failed for current_state")
    draft = deps.kv_get("draft") or []
    return {
        "history": (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:],
        "draft": draft,
        "push_status": _push_status(deps, _parse_draft(draft, deps.now().date())),
        "goals": deps.kv_get("goals") or "",
        "readiness": readiness,
        "pain": pain,
        "today": deps.now().date().isoformat(),
    }
