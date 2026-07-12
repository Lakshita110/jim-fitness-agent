"""The single truly generative step: {state + goals + constraints} -> a
structured session as schema-validated JSON (PLAN.md §4).

Everything the model sees is a compact summary produced by the tools; it never
sees raw API rows. Output is forced to JSON and parsed into StructuredSession;
parse failures raise so the loop can retry/fall back deterministically."""

import json
from datetime import date

from jim.config import (
    FORBIDDEN_EXERCISES,
    MAX_SESSION_MIN,
    MIN_DAYS_BETWEEN_LEG_SESSIONS,
    OPENROUTER_BASE_URL,
)
from jim.schemas import (
    GarminToday,
    HistoryFeatures,
    NotionDay,
    ResearchHit,
    StructuredSession,
)

SYSTEM_PROMPT = """You are a careful strength & conditioning coach for a single athlete
with knee and ankle constraints. Propose ONE session for tomorrow as JSON only.

You are given the athlete's PLAYBOOK: their base A/B/C strength rotation, two PT
routines, and standing directives. PREFER selecting a base template unchanged —
when you do, copy its garmin_workout_id and template_key into your response and
leave `steps` empty (the existing Garmin workout will be scheduled as-is, with
its loaded weights). Only hand-build `steps` when pain/recovery forces you to
adapt or substitute.

You may also be given the athlete's LONG-TERM GOALS — the direction they're
training toward. Weave them into the choice (progressions, deloads, milestones)
without ever breaking the hard rules or the pain guardrail.

Hard rules (never violate; the playbook directives sit below these):
- Never program: {forbidden}.
- Keep the session under {max_min} minutes.
- Leg sessions need at least {leg_gap} days since the last leg session.
- Respect low readiness / pain: on bad days prefer PT, mobility, or easy conditioning.
- If research snippets are provided, ground any substitutions in them.

Respond with a single JSON object matching:
{{"for_date": "YYYY-MM-DD", "kind": "strength|conditioning|mobility|rest",
  "title": str, "template_key": str|null, "garmin_workout_id": str|null,
  "steps": [{{"exercise": str, "sets": int, "reps": int|null,
             "duration_sec": int|null, "weight_kg": float|null, "notes": str}}],
  "est_duration_min": float, "rationale_summary": str}}"""


def build_user_prompt(
    for_date: date,
    garmin: GarminToday,
    notion: NotionDay,
    features: HistoryFeatures,
    research: list[ResearchHit],
    revision_feedback: list[str] | None = None,
    playbook_text: str = "",
    goals_text: str = "",
) -> str:
    parts = [
        f"Propose the session for {for_date.isoformat()}.",
        f"Today (Garmin): {garmin.model_dump_json()}",
        f"Today (log): {notion.model_dump_json()}",
        f"History features: {features.model_dump_json()}",
    ]
    if goals_text:
        parts.append("# LONG-TERM GOALS\n" + goals_text)
    if playbook_text:
        parts.append("# PLAYBOOK\n" + playbook_text)
    if research:
        parts.append(
            "Research snippets:\n"
            + "\n".join(f"- [{h.source}] {h.title}: {h.snippet}" for h in research)
        )
    if revision_feedback:
        parts.append(
            "Your previous proposal was REJECTED by the validator. Fix these and"
            " re-propose:\n" + "\n".join(f"- {v}" for v in revision_feedback)
        )
    return "\n\n".join(parts)


def compose_session(
    for_date: date,
    garmin: GarminToday,
    notion: NotionDay,
    features: HistoryFeatures,
    research: list[ResearchHit],
    model: str,
    revision_feedback: list[str] | None = None,
    playbook_text: str = "",
    goals_text: str = "",
) -> StructuredSession:
    from openai import OpenAI

    from jim.config import settings

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=settings().openrouter_api_key)
    system = SYSTEM_PROMPT.format(
        forbidden=", ".join(FORBIDDEN_EXERCISES),
        max_min=MAX_SESSION_MIN,
        leg_gap=MIN_DAYS_BETWEEN_LEG_SESSIONS,
    )
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": build_user_prompt(
                    for_date, garmin, notion, features, research,
                    revision_feedback, playbook_text, goals_text,
                ),
            },
        ],
    )
    return parse_session(resp.choices[0].message.content or "", for_date)


def parse_session(raw: str, for_date: date) -> StructuredSession:
    data = json.loads(raw)
    data["for_date"] = for_date.isoformat()  # never trust the model's date
    return StructuredSession.model_validate(data)
