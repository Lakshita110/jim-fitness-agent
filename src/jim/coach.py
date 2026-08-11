"""Jim's coach chat: one conversation thread (single user) to iterate on a
plan for tomorrow or the week, keep long-term goals in plain language, and
push to Garmin only on explicit approve.

The model can call TOOLS mid-turn (bounded to MAX_TOOL_ROUNDS): read-only
lookups — per-exercise performance history from the watch (checked before
prescribing weights), recent workout/adherence history, and research (curated
corpus + web) — plus two mutating actions, both explicit-request-only:
promote_workout_to_playbook, which saves an already-pushed adaptation as a
new permanent playbook default (see playbook.promote_garmin_workout), and
update_playbook_workout, which edits an existing template in place (see
playbook.update_workout_template). A one-time workout or one-time tweak to a
single day never calls either — it's just an adapted draft session, built as
a disposable one-off Garmin workout that's cleaned up automatically once its
date passes (see jobs.nightly.cleanup_stale_adaptations). The tool
round-trip itself isn't persisted to chat history — the model's own reply is
the durable record of what it did, so it must say so in plain language.

State is deliberately simple — everything lives in the kv store:
- 'chat_history': last HISTORY_LIMIT messages [{role, content}]
- 'draft': the working plan, a list of StructuredSession dicts (dated days)
- 'goals': plain-text long-term goals block, rewritten by the model on request
- 'state': cached day snapshot (garmin/features), refreshed hourly

Deps are injected (`CoachDeps`) so everything unit-tests without Postgres,
Garmin, or an LLM."""

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
from jim.playbook import Playbook, _load_playbook_from_disk, use_existing_workout
from jim.schemas import HistoryFeatures, StructuredSession

log = logging.getLogger(__name__)

HISTORY_LIMIT = 30
STATE_TTL_MIN = 60
DRAFT_MAX_DAYS = 7
MAX_REPLY_CHARS = 3800
MAX_TOOL_ROUNDS = 4  # lookup rounds per turn — keeps cost bounded

# Tools the model may call mid-conversation (OpenAI function schemas) — the
# first three are read-only lookups, the last is a mutating action.
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
    {
        "type": "function",
        "function": {
            "name": "promote_workout_to_playbook",
            "description": "Save a day's already-pushed adaptation as a new permanent"
            " playbook default (e.g. 'make this my new Full Body A'). Only call this"
            " when the athlete EXPLICITLY asks to keep an adapted session as their new"
            " default — never on your own initiative. The day must already be on"
            " Garmin (pushed from the draft) before this works.",
            "parameters": {
                "type": "object",
                "properties": {
                    "for_date": {"type": "string",
                                 "description": "YYYY-MM-DD of the pushed adaptation"},
                    "key": {"type": "string",
                            "description": "playbook key, e.g. 'full_body_a'"},
                    "add_to_rotation": {"type": "boolean",
                                        "description": "also add this key to the A/B/C rotation"},
                },
                "required": ["for_date", "key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_playbook_workout",
            "description": "Create a new permanent playbook workout, or edit an existing"
            " one in place (e.g. 'swap goblet squats into Full Body A for good', or"
            " 'add an upper-body day'). Call once per workout — several calls in one"
            " turn is how you build a whole new program. Only call this when the athlete"
            " EXPLICITLY asks to change their standing templates, never on your own"
            " initiative. When editing, include only the fields being changed; omitted"
            " fields are left as-is. Creating requires label and sport. Changing warmup"
            " or blocks clears the stored Garmin workout ID, so it's rebuilt fresh from"
            " the new steps next time it's scheduled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "playbook key, e.g. 'upper_a' — snake_case,"
                            " stable, used to reference this workout in the rotation"},
                    "label": {"type": "string",
                              "description": "human name, e.g. 'Upper A'; required when"
                              " creating"},
                    "sport": {"type": "string",
                              "enum": ["strength_training", "mobility", "cardio"],
                              "description": "required when creating"},
                    "warmup": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "reps": {"type": "integer"},
                                "time_sec": {"type": "integer"},
                            },
                            "required": ["name"],
                        },
                    },
                    "blocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "group": {"type": "string"},
                                "sets": {"type": "integer",
                                         "description": "rounds for the whole block"},
                                "exercises": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "sets": {"type": "integer"},
                                            "reps": {"type": "integer"},
                                            "time_sec": {"type": "integer"},
                                        },
                                        "required": ["name"],
                                    },
                                },
                            },
                            "required": ["exercises"],
                        },
                    },
                    "equipment": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_playbook_rotation",
            "description": "Replace the rotation — the ordered list of workouts cycled"
            " through on training days (e.g. 'switch me to a 4-day upper/lower split')."
            " Pass the FULL new order, not just additions; it replaces what's there."
            " Every key must already exist in the playbook, so call"
            " save_playbook_workout first for any workout you're inventing. Only call"
            " this when the athlete EXPLICITLY asks to change their program structure,"
            " never on your own initiative.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "playbook keys in the order they should cycle;"
                        " [] clears the rotation",
                    },
                },
                "required": ["keys"],
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
- Respect low readiness: prefer PT, mobility, or easy conditioning on bad days.

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
- Only ever use a garmin_workout_id/template_key that appears in the PLAYBOOK
  block below — never invent one, even a plausible-looking one, and never
  reuse an ID from an earlier turn once it's not listed there. If there's no
  matching template for what you want to schedule, write it as an adaptation
  with full steps instead.

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

ONE-OFF SESSIONS: a new one-time workout, or a one-time change to an existing
day ("just today, swap in X" / "give me a random conditioning session for
tomorrow"), is just an adapted draft day — full steps, no garmin_workout_id.
Pushing it builds a disposable one-off Garmin workout and never touches the
playbook; it's cleaned up automatically once its date passes. Don't call any
playbook tool for these — only do that on an explicit, standing request.

PROMOTING AN ADAPTATION: if (and ONLY if) the athlete explicitly asks to keep
an already-pushed adaptation as their new default (e.g. "make this my new
Full Body A", "let's lock this in"), call promote_workout_to_playbook with
that day's date and a playbook key. Never call it on your own initiative,
never for a day that hasn't been pushed yet.

CHANGING THE PLAYBOOK ITSELF: only ever on an explicit, standing request —
never on your own initiative. To permanently change a template ("update my
Full Body A to use goblet squats from now on"), call save_playbook_workout
with that key and only the fields being changed. To invent one, call it with
a new key plus label and sport. To restructure the program ("switch me to a
4-day upper/lower split"), save each workout first, then call
set_playbook_rotation with the FULL new order — you may make all of those
calls in one turn. Always confirm in your reply exactly what you changed —
that reply is the only durable record of the action.

ROTATION: the ROTATION block below tells you which template was last done and
the order to continue in. Follow that order for training days unless
readiness, pain, or an explicit request says otherwise — and say one plain
line when you deviate.

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
    fetch_state: Callable[[], dict]  # fresh garmin/features snapshot
    # (messages, tool_schemas|None) -> {"content": str|None, "tool_calls": [...]|None}
    llm: Callable[[list[dict], list[dict] | None], dict]
    lookup_tools: dict[str, Callable[..., str]]  # name -> callable, see TOOL_SCHEMAS
    schedule_workout: Callable[..., None]
    clear_schedule: Callable[..., None]  # unschedule planned workouts on a date
    create_garmin_workout: Callable[..., Any]
    delete_garmin_workout: Callable[[str], None]
    record_suggestion: Callable[..., int]
    playbook_text: Callable[[], str]
    now: Callable[[], datetime]
    # Only consulted when a day carries a template ID, to tell an untouched
    # template apart from an adapted one before pushing. Default is the plain
    # disk loader (not the now-user-scoped load_playbook) purely so tests that
    # construct CoachDeps directly without injecting `playbook` keep working
    # with zero args — .live() always overrides this with the real per-user one.
    playbook: Callable[[], Playbook] = _load_playbook_from_disk
    # (last rotation template_key, ISO date it was pushed) — what the prompt's
    # ROTATION block is built from. Defaults to "no history", i.e. start at the
    # top of the rotation, so directly-constructed test deps keep working.
    rotation_state: Callable[[], tuple[str | None, str | None]] = lambda: (None, None)

    @classmethod
    def live(cls, user_id: int) -> "CoachDeps":
        from zoneinfo import ZoneInfo

        from jim.config import settings
        from jim.db import kv_get, kv_set
        from jim.playbook import load_playbook
        from jim.tools import garmin, memory
        from jim.tools.history import (
            exercise_history,
            last_rotation_key,
            query_history,
            readiness_read,
            workout_history,
        )

        def now() -> datetime:
            return datetime.now(ZoneInfo(settings().app_timezone))

        def fetch_state() -> dict:
            today = now().date()
            # Each source degrades independently — a down integration must
            # not blank Garmin, features, or readiness.
            sources = {
                "garmin": lambda: garmin.get_garmin_today(user_id, today),
                "features": lambda: query_history(user_id, today),
                "readiness": lambda: readiness_read(user_id, today),
                # dates serialized to ISO here — the kv store and the prompt's
                # json.dumps(state) can't carry a raw date object.
                "calendar": lambda: [
                    {**item, "date": item["date"].isoformat()}
                    for item in garmin.get_scheduled_workouts(
                        user_id, today, today + timedelta(days=DRAFT_MAX_DAYS - 1)
                    )
                ],
            }
            state: dict = {}
            for name, fetch in sources.items():
                try:
                    result = fetch()
                    # every other source is a pydantic model; calendar is a plain
                    # list already shaped for storage/JSON.
                    state[name] = result if isinstance(result, list) else result.model_dump(
                        mode="json"
                    )
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

        def promote_workout_to_playbook(
            for_date: str, key: str, add_to_rotation: bool = False,
        ) -> str:
            from jim.playbook import promote_garmin_workout

            created = kv_get(user_id, "jim_created_workouts") or {}
            entry = created.get(for_date)
            if not entry:
                return (
                    f"No one-off adaptation on file for {for_date} — it may not have"
                    " been pushed to Garmin yet, or it was already promoted."
                )
            template = promote_garmin_workout(
                user_id, entry["workout_id"], key,
                add_to_rotation=add_to_rotation,
            )
            return f"Saved {for_date}'s adaptation as the new '{key}' template ({template.label})."

        def save_playbook_workout(
            key: str, label: str | None = None, sport: str | None = None,
            warmup: list[dict] | None = None, blocks: list[dict] | None = None,
            equipment: list[str] | None = None,
        ) -> str:
            from jim.playbook import Block, Exercise, save_workout_template

            existed = key in load_playbook(user_id).workouts
            try:
                template = save_workout_template(
                    user_id, key,
                    label=label, sport=sport,
                    warmup=[Exercise(**e) for e in warmup] if warmup is not None else None,
                    blocks=[Block(**b) for b in blocks] if blocks is not None else None,
                    equipment=equipment,
                )
            except RuntimeError as e:
                return str(e)
            verb = "Updated" if existed else "Created"
            return f"{verb} playbook workout '{key}' ({template.label})."

        def set_playbook_rotation(keys: list[str]) -> str:
            from jim.playbook import set_rotation

            try:
                rotation = set_rotation(user_id, keys)
            except RuntimeError as e:
                return str(e)
            return "Rotation is now: " + (" → ".join(rotation) or "(empty)")

        def rotation_state() -> tuple[str | None, str | None]:
            last_key, on = last_rotation_key(
                user_id, load_playbook(user_id).rotation, now().date()
            )
            return (last_key, on.isoformat() if on else None)

        return cls(
            kv_get=lambda key: kv_get(user_id, key),
            kv_set=lambda key, value: kv_set(user_id, key, value),
            fetch_state=fetch_state,
            llm=llm,
            lookup_tools={
                "exercise_history": lambda exercise: exercise_history(user_id, exercise),
                "workout_history": lambda days=14: workout_history(user_id, days),
                "research": research,
                "promote_workout_to_playbook": promote_workout_to_playbook,
                "save_playbook_workout": save_playbook_workout,
                "set_playbook_rotation": set_playbook_rotation,
            },
            schedule_workout=lambda wid, on: garmin.schedule_workout(user_id, wid, on),
            clear_schedule=lambda on: garmin.clear_schedule(user_id, on),
            create_garmin_workout=lambda s: garmin.create_garmin_workout(user_id, s),
            delete_garmin_workout=lambda wid: garmin.delete_garmin_workout(user_id, wid),
            record_suggestion=lambda *a, **kw: memory.record_suggestion(user_id, *a, **kw),
            playbook_text=lambda: load_playbook(user_id).to_prompt(),
            playbook=lambda: load_playbook(user_id),
            now=now,
            rotation_state=rotation_state,
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


def _maybe_sync_calendar(deps: CoachDeps, state: dict) -> None:
    """Run the calendar->draft sync if this state snapshot carries one. Cheap
    no-op most turns: `state["calendar"]` only refreshes hourly (it rides the
    same cache as everything else), and the sync itself is a no-op once every
    calendar day is already covered by the draft."""
    calendar = state.get("calendar")
    if calendar:
        try:
            _sync_calendar_into_draft(deps, calendar)
        except Exception:
            log.exception("calendar sync failed; draft left as-is")


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


# A template's `sport` field (as written in the playbook YAML) to the session
# `kind` the model/chat use — not the same vocabulary as Garmin's SPORT_TYPES
# keys, though they overlap. Anything unrecognized falls back to "strength".
_SPORT_TO_KIND: dict[str, str] = {
    "strength": "strength", "strength_training": "strength",
    "mobility": "mobility", "conditioning": "conditioning",
}


def _session_from_calendar_item(item: dict, playbook: Playbook) -> StructuredSession:
    """A calendar item already on the real Garmin schedule, as a draft day.

    Always a template pick (steps=[]) — the workout exists on Garmin already,
    so scheduling it by ID is the only correct move (see
    playbook.use_existing_workout). The calendar's own title wins over the
    template's label even when a template matches, since the athlete may have
    hand-edited the title directly on Garmin (e.g. "Full Body A (modified)")."""
    wt = playbook.by_workout_id(item["workout_id"])
    kind = _SPORT_TO_KIND.get(wt.sport, "strength") if wt else "strength"
    return StructuredSession(
        for_date=item["date"],
        kind=kind,
        title=item["title"],
        template_key=wt.key if wt else None,
        garmin_workout_id=item["workout_id"],
        steps=[],
        est_duration_min=0,
        rationale_summary="Already scheduled on your Garmin calendar.",
    )


def _sync_calendar_into_draft(deps: CoachDeps, calendar_items: list[dict]) -> None:
    """Fill empty draft days from the real Garmin calendar — never touches a day
    Jim or the athlete already planned. "Drafts merge, they don't replace" and
    "the athlete's plan wins" apply here too: a calendar read is the lowest-
    priority source, so it only ever fills gaps, never overwrites."""
    today = deps.now().date()
    existing = _parse_draft(deps.kv_get("draft") or [], today)
    have = {s.for_date.isoformat() for s in existing}
    playbook = deps.playbook()

    added = [
        _session_from_calendar_item(item, playbook)
        for item in calendar_items
        if str(item["date"]) not in have
    ]
    if not added:
        return

    merged = existing + added
    deps.kv_set("draft", [s.model_dump(mode="json") for s in merged])

    pushed = deps.kv_get("pushed") or {}
    for s in added:
        fd = s.for_date.isoformat()
        pushed[fd] = {"title": s.title, "sig": _sig(s), "pushed_at": deps.now().isoformat()}
    deps.kv_set("pushed", pushed)


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


def adaptation_title(template_label: str | None, for_date: date, fallback: str) -> str:
    """Deterministic title for a one-off adaptation — never left to the model,
    so provenance (this is disposable, not a real template) is visible both in
    Jim's own Garmin-workout picker and in the native Garmin Connect app."""
    base = template_label or fallback
    return f"{base} — adapted {for_date.isoformat()}"


def _push_one(deps: CoachDeps, session: StructuredSession) -> tuple[bool, str]:
    """Schedule a single session on the watch and record it. Returns
    (pushed, summary line) — callers must only mark the day as on-watch when
    `pushed` is true. Without that check a refused push (see below) still got
    recorded as successful, so the UI showed a day as on-watch that Garmin had
    never actually received.

    An untouched template is scheduled by ID (its loaded weights live on Garmin,
    not here); anything the athlete adapted is built fresh from its steps. The
    steps are what they can see in the plan, so the steps are what must land on
    the watch — see playbook.use_existing_workout.

    An adaptation always creates a brand-new Garmin workout (there's no update
    path), so each one is tracked in the "jim_created_workouts" kv entry —
    same shape/spirit as "pushed" in _mark_pushed — until it's either promoted
    into the playbook (via the Garmin-import route) or swept by the nightly
    cleanup once its day has passed. Re-pushing the same date deletes the
    prior one-off first so the athlete's Garmin library doesn't accumulate
    dupes ("Full Body A (modified)", "Full Body A (modified)", ...)."""
    fd = session.for_date
    fd_iso = fd.isoformat()
    as_template = bool(
        session.kind != "rest"
        and session.garmin_workout_id  # short-circuits: no template ID, no playbook read
        and use_existing_workout(session, deps.playbook())
    )
    if session.kind != "rest" and not as_template and not session.steps:
        # A template pick (empty steps) whose ID/key doesn't resolve against
        # THIS playbook — the model invented a template that isn't there.
        # Building from empty steps would create a garbage Garmin workout;
        # scheduling the unverified ID risks landing on an unrelated real one.
        # Refuse rather than touch Garmin at all.
        return (False,
                f"{fd}: couldn't push — {session.title!r} doesn't match any"
                " playbook workout and has no steps to build from")
    if session.kind == "rest":
        pass  # rest schedules nothing on the watch
    elif as_template:
        deps.schedule_workout(session.garmin_workout_id, fd)
    else:
        created = deps.kv_get("jim_created_workouts") or {}
        prior = created.get(fd_iso)
        if prior:
            try:
                deps.delete_garmin_workout(prior["workout_id"])
            except Exception:
                log.warning("couldn't delete prior adaptation %s for %s",
                            prior["workout_id"], fd_iso, exc_info=True)
        template_label = None
        if session.template_key:
            wt = deps.playbook().template(session.template_key)
            template_label = wt.label if wt else None
        titled = session.model_copy(update={
            "title": adaptation_title(template_label, fd, session.title),
        })
        ref = deps.create_garmin_workout(titled)
        deps.schedule_workout(ref.workout_id, fd)
        created[fd_iso] = {
            "workout_id": ref.workout_id,
            "template_key": session.template_key,
            "created_ts": deps.now().isoformat(),
        }
        deps.kv_set("jim_created_workouts", created)
    deps.record_suggestion(
        fd, session, session.rationale_summary, False, "fast", source="chat",
    )
    if session.kind == "rest":
        return (True, f"{fd}: rest day (nothing scheduled)")
    verb = "scheduled" if as_template else "created + scheduled"
    return (True, f"{fd}: {verb} {session.title}")


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
    ]
    rotation_block = _rotation_block(deps, today)
    if rotation_block:
        parts.append(rotation_block)
    parts.append("# PLAYBOOK\n" + deps.playbook_text())
    return "\n\n".join(parts)


def _rotation_block(deps: CoachDeps, today: date) -> str:
    """Which template was last done and the order to continue in.

    Without this the model has to guess the next letter from workout titles;
    with it, a whole week sequences correctly in one turn — which is why the
    full continuing order is spelled out, not just the next key. Empty string
    when there's no rotation to follow (a fresh signup), so the prompt doesn't
    carry a hollow section."""
    rotation = deps.playbook().rotation
    if not rotation:
        return ""

    last_key, last_on = deps.rotation_state()
    order = deps.playbook().rotation_from(last_key)
    lines = ["# ROTATION"]
    if last_key and last_on:
        ago = (today - date.fromisoformat(last_on)).days
        when = "today" if ago == 0 else f"{ago} day{'s' if ago != 1 else ''} ago"
        lines.append(f"Last rotation workout: {last_key}, pushed {last_on} ({when})")
    else:
        lines.append("No rotation workout on record yet — start at the top.")
    lines.append("Continue in this order: " + " → ".join(order))
    return "\n".join(lines)


def _loads_json(text: str) -> dict:
    """Lenient JSON parse — strips markdown fences some models emit."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned)


def _run_model(deps: CoachDeps, system: str, history: list[dict]) -> dict:
    """One model turn, with a bounded tool loop: the model may call
    exercise_history / workout_history / research (read-only) or
    promote_workout_to_playbook / save_playbook_workout /
    set_playbook_rotation (mutating) before answering."""
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


def converse(text: str, user_id: int, deps: CoachDeps | None = None,
             scope_date: str | None = None) -> dict:
    """One chat turn. Returns {reply, draft, push_status} and persists
    history/draft/goals. `scope_date` (an ISO date) narrows the edit to a single
    day — the model is told to return only that day, merged onto the plan."""
    deps = deps or CoachDeps.live(user_id)
    today = deps.now().date()
    state = _cached_state(deps)
    _maybe_sync_calendar(deps, state)
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
                # Drop yesterday's leftovers BEFORE truncating. The cap keeps
                # the earliest dates, so a stale past day would otherwise
                # silently evict a real future one — and the UI's week always
                # starts at today, so past days aren't even visible.
                live = [k for k in sorted(by_date) if k >= today.isoformat()]
                return [by_date[k] for k in live][:DRAFT_MAX_DAYS]

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


def plan_week(user_id: int, deps: CoachDeps | None = None,
              days: int = DRAFT_MAX_DAYS) -> dict:
    """Fill the next `days` of the draft in one turn, leaving days already on
    the watch untouched.

    Funnels through converse(), so there's still exactly one planning path —
    this only supplies the instruction and re-asserts the protected days
    afterwards. That re-assertion, not the prompt wording, is what actually
    guarantees a pushed day survives: a model that ignores the instruction
    still can't move it.

    Note the deliberate asymmetry with typed chat, which stays unrestricted.
    Asking for a re-plan in words is the athlete overriding on purpose; the
    button is the safe bulk action.

    Returns converse()'s shape plus `prompt` — the instruction used, so the UI
    can show it as the athlete's message without duplicating the wording."""
    deps = deps or CoachDeps.live(user_id)
    today = deps.now().date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(days)]
    dates_set = set(dates)

    # Restricted to THIS window: `pushed` can carry long-stale entries (a
    # day pushed weeks ago that never got pruned from the draft) — without
    # this filter those leak into the prompt as "already on your watch,
    # leave alone" for dates nowhere near the week being planned.
    protected = set(deps.kv_get("pushed") or {}) & dates_set
    before = {
        s.for_date.isoformat(): s
        for s in _parse_draft(deps.kv_get("draft") or [], today)
        if s.for_date.isoformat() in protected
    }

    locked = sorted(before)
    open_dates = [d for d in dates if d not in before]
    prompt = (
        f"Plan my training for {dates[0]} through {dates[-1]}."
        f" Fill every one of these dates: {', '.join(open_dates)}."
        " Include rest days explicitly as kind \"rest\"."
    )
    if locked:
        prompt += (
            f" Leave {', '.join(locked)} exactly as they are — they're already"
            " on my watch. Plan around them."
        )

    out = converse(prompt, user_id, deps)

    if before:
        plan = _parse_draft(out.get("draft") or [], today)
        merged = {s.for_date.isoformat(): s for s in plan}
        merged.update(before)  # the model doesn't get a vote on these
        restored = [merged[k] for k in sorted(merged)][:DRAFT_MAX_DAYS]
        deps.kv_set("draft", [s.model_dump(mode="json") for s in restored])
        out["draft"] = deps.kv_get("draft") or []
        out["push_status"] = _push_status(deps, restored)

    out["prompt"] = prompt
    return out


def approve(user_id: int, deps: CoachDeps | None = None) -> str:
    """Push every day in the draft to Garmin and record suggestions. The draft
    is kept (each day now shows as on-watch) so it stays visible and editable;
    already-pushed days are re-scheduled cleanly (unschedule first)."""
    deps = deps or CoachDeps.live(user_id)
    draft = _parse_draft(deps.kv_get("draft") or [], deps.now().date())
    if not draft:
        return "Nothing to push — the draft is empty."
    pushed_before = deps.kv_get("pushed") or {}
    lines = []
    for session in draft:
        fd = session.for_date.isoformat()
        if fd in pushed_before and session.kind != "rest":
            deps.clear_schedule(session.for_date)  # replace, don't duplicate
        ok, line = _push_one(deps, session)
        lines.append(line)
        if ok:
            _mark_pushed(deps, session)
    summary = "Pushed to Garmin:\n" + "\n".join(lines)
    history = (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:]
    history.append({"role": "assistant", "content": summary})
    deps.kv_set("chat_history", history)
    return summary


def push_day(for_date: str, user_id: int, deps: CoachDeps | None = None) -> dict:
    """Push (or update) a single draft day to Garmin. Returns
    {summary, draft, push_status}. Re-pushing an already-pushed day unschedules
    the prior one first so the watch never ends up with a duplicate."""
    deps = deps or CoachDeps.live(user_id)
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
        ok, line = _push_one(deps, session)
        if ok:
            _mark_pushed(deps, session)
            verb = "Updated on Garmin" if updating else "Pushed to Garmin"
            summary = f"{verb} — {line.split(': ', 1)[-1]}"
        else:
            summary = line
    return {"summary": summary, "draft": draft_json,
            "push_status": _push_status(deps, draft)}


def clear(user_id: int, deps: CoachDeps | None = None) -> None:
    """Start a fresh conversation (draft and goals survive)."""
    deps = deps or CoachDeps.live(user_id)
    deps.kv_set("chat_history", [])


def current_state(user_id: int, deps: CoachDeps | None = None) -> dict:
    """What the UI shows on load: recent messages + working draft + goals,
    plus the readiness verdict for the stat cards."""
    deps = deps or CoachDeps.live(user_id)
    readiness = None
    try:  # a state hiccup must never break the page load
        state = _cached_state(deps)
        _maybe_sync_calendar(deps, state)
        readiness = state.get("readiness")
    except Exception:
        log.exception("state read failed for current_state")
    draft = deps.kv_get("draft") or []
    return {
        "history": (deps.kv_get("chat_history") or [])[-HISTORY_LIMIT:],
        "draft": draft,
        "push_status": _push_status(deps, _parse_draft(draft, deps.now().date())),
        "goals": deps.kv_get("goals") or "",
        "readiness": readiness,
        "today": deps.now().date().isoformat(),
    }
