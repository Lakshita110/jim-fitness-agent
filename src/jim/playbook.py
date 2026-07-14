"""Playbook — the durable, human-editable memory layer (see playbook/).

Three files, loaded into the agent's context every night:
- base_workouts.yaml : the A/B/C strength rotation (references Garmin IDs)
- pt_routines.yaml   : home + gym PT for non-lifting days
- directives.md      : standing instructions the user edits in plain English

This is the "give instructions to the agent" surface. Editing a file changes
the next run — no code change, no DB write. The loader validates structure and
renders a compact text block for the compose prompt; `garmin_workout_id` lets
the loop schedule an existing Garmin workout directly instead of rebuilding it."""

import json
import logging
from pathlib import Path

import yaml
from pydantic import BaseModel

from jim.schemas import StructuredSession

log = logging.getLogger(__name__)

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent.parent / "playbook"
DEFAULT_PLAYBOOK_DIR = PLAYBOOK_DIR / "defaults"


class Exercise(BaseModel):
    name: str
    sets: int | None = None
    reps: int | None = None
    time_sec: int | None = None
    tags: list[str] = []
    equipment: list[str] = []


class Block(BaseModel):
    group: str | None = None
    sets: int | None = None  # rounds for the whole block (strength supersets)
    exercises: list[Exercise] = []


class WorkoutTemplate(BaseModel):
    key: str
    label: str
    garmin_workout_id: str | None = None
    sport: str
    equipment: list[str] = []
    warmup: list[Exercise] = []
    blocks: list[Block] = []


class Playbook(BaseModel):
    rotation: list[str] = []
    workouts: dict[str, WorkoutTemplate] = {}
    pt_routines: dict[str, WorkoutTemplate] = {}
    directives: str = ""

    def template(self, key: str) -> WorkoutTemplate | None:
        return self.workouts.get(key) or self.pt_routines.get(key)

    def by_workout_id(self, workout_id: str) -> WorkoutTemplate | None:
        """Reverse lookup — the model reliably echoes the Garmin ID even when it
        forgets (or invents) the template_key."""
        for wt in (*self.workouts.values(), *self.pt_routines.values()):
            if wt.garmin_workout_id == workout_id:
                return wt
        return None

    def next_in_rotation(self, last_key: str | None) -> str | None:
        """The letter after `last_key` in the A/B/C cycle (wraps)."""
        if not self.rotation:
            return None
        if last_key not in self.rotation:
            return self.rotation[0]
        i = self.rotation.index(last_key)
        return self.rotation[(i + 1) % len(self.rotation)]

    def to_prompt(self) -> str:
        """Compact rendering for the compose prompt — the model sees names,
        doses, and tags, not raw YAML."""
        lines = ["## Base strength rotation (schedule the Garmin ID as-is on lifting days)"]
        lines.append(f"Rotation order: {' → '.join(self.rotation)}")
        for key in self.rotation:
            wt = self.workouts.get(key)
            if wt:
                lines.append(_render_template(wt))
        lines.append("\n## PT routines (non-lifting days)")
        for wt in self.pt_routines.values():
            lines.append(_render_template(wt))
        if self.directives:
            lines.append("\n## Standing directives (obey these)\n" + self.directives)
        return "\n".join(lines)


def _key(name: str) -> str:
    return " ".join(name.lower().split())


def template_prescription(wt: WorkoutTemplate) -> list[tuple[str, int, int | None, int | None]]:
    """The template's own steps as (name, sets, reps, seconds).

    Block-level `sets` are the rounds for every exercise in that block, so they
    flatten onto each exercise — that's the shape a model produces when it
    restates a template instead of adapting it."""
    rows = [(_key(ex.name), ex.sets or 1, ex.reps, ex.time_sec) for ex in wt.warmup]
    for block in wt.blocks:
        rounds = block.sets or 1
        rows += [
            (_key(ex.name), ex.sets or rounds, ex.reps, ex.time_sec)
            for ex in block.exercises
        ]
    return rows


def use_existing_workout(session: StructuredSession, playbook: "Playbook") -> bool:
    """Whether pushing this day should schedule the EXISTING Garmin workout by ID
    instead of building a new one from `session.steps`.

    Only when the day really is the template: it carries no steps (the contract
    the model is given), or its steps merely restate the template's own
    prescription. The moment they diverge — a swap, a dropped move, a prescribed
    weight — the day is an ADAPTATION and must be built fresh.

    This is enforced here rather than trusted to the prompt because the model
    routinely echoes a template's garmin_workout_id alongside its edits. Reading
    the ID first meant those edits were silently discarded and stock Full Body A
    landed on the watch instead."""
    if not session.garmin_workout_id:
        return False
    if not session.steps:
        return True

    wt = playbook.template(session.template_key or "") or playbook.by_workout_id(
        session.garmin_workout_id
    )
    if wt is None:
        return False  # unknown template but explicit steps — trust the steps
    if any(step.weight_kg is not None for step in session.steps):
        return False  # templates carry no loads, so a prescribed weight is an edit
    prescribed = [
        (_key(s.exercise), s.sets, s.reps, s.duration_sec) for s in session.steps
    ]
    return prescribed == template_prescription(wt)


def _dose(ex: Exercise) -> str:
    parts = []
    if ex.sets:
        parts.append(f"{ex.sets}x")
    if ex.reps:
        parts.append(f"{ex.reps}")
    elif ex.time_sec:
        parts.append(f"{ex.time_sec}s")
    dose = "".join(parts) if parts else ""
    tag = f" [{','.join(ex.tags)}]" if ex.tags else ""
    return f"{ex.name} {dose}".strip() + tag


def _render_template(wt: WorkoutTemplate) -> str:
    head = f"\n### {wt.label}"
    if wt.garmin_workout_id:
        head += f" (garmin_workout_id={wt.garmin_workout_id})"
    lines = [head]
    for block in wt.blocks:
        prefix = f"- {block.group}: " if block.group else "- "
        rounds = f"[{block.sets} rounds] " if block.sets else ""
        items = "; ".join(_dose(e) for e in block.exercises)
        lines.append(f"{prefix}{rounds}{items}")
    return "\n".join(lines)


def _load_playbook_from_disk(directory: Path = PLAYBOOK_DIR) -> Playbook:
    """The original disk-reading loader. Kept as the seed source for the
    one-off athlete backfill (scripts/backfill_users.py) — per-user storage
    is now Postgres (`load_playbook(user_id)` below)."""
    # Always utf-8: the playbook is full of em dashes and degree signs, and
    # read_text() defaults to the locale encoding (cp1252 on Windows), which
    # mangles them into the prompt, the exercise match, and the watch.
    base = yaml.safe_load((directory / "base_workouts.yaml").read_text("utf-8")) or {}
    pt = yaml.safe_load((directory / "pt_routines.yaml").read_text("utf-8")) or {}
    directives_path = directory / "directives.md"
    directives = directives_path.read_text("utf-8") if directives_path.exists() else ""

    workouts = {
        key: WorkoutTemplate(key=key, **spec)
        for key, spec in (base.get("workouts") or {}).items()
    }
    pt_routines = {
        key: WorkoutTemplate(key=key, **spec)
        for key, spec in (pt.get("routines") or {}).items()
    }
    return Playbook(
        rotation=base.get("rotation", []),
        workouts=workouts,
        pt_routines=pt_routines,
        directives=_strip_html_comments(directives),
    )


def _load_default_playbook() -> Playbook:
    """The generic seed for a brand-new signup (playbook/defaults/) — not the
    committed athlete YAML, which is this one athlete's own knee-specific
    content. Used by auth.create_user()."""
    return _load_playbook_from_disk(DEFAULT_PLAYBOOK_DIR)


def load_playbook(user_id: int) -> Playbook:
    """Per-user playbook, stored in Postgres (`playbooks` table, one row per
    user, JSONB columns — see soft-baking-kettle plan §5)."""
    from jim.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT rotation, workouts, pt_routines, directives FROM playbooks"
            " WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if row is None:
        return Playbook()  # safety net; a row should exist post-signup
    workouts = {k: WorkoutTemplate(key=k, **v) for k, v in row["workouts"].items()}
    pt_routines = {k: WorkoutTemplate(key=k, **v) for k, v in row["pt_routines"].items()}
    return Playbook(
        rotation=row["rotation"], workouts=workouts, pt_routines=pt_routines,
        directives=row["directives"],
    )


def save_playbook(user_id: int, pb: Playbook) -> None:
    """Upsert `pb` for `user_id`. Phase 4's /api/playbook POST route calls this."""
    from jim.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO playbooks (user_id, rotation, workouts, pt_routines, directives,"
            " updated_ts) VALUES (%s, %s, %s, %s, %s, now())"
            " ON CONFLICT (user_id) DO UPDATE SET rotation = EXCLUDED.rotation,"
            " workouts = EXCLUDED.workouts, pt_routines = EXCLUDED.pt_routines,"
            " directives = EXCLUDED.directives, updated_ts = now()",
            (
                user_id,
                json.dumps(pb.rotation),
                json.dumps({k: v.model_dump(mode="json") for k, v in pb.workouts.items()}),
                json.dumps({k: v.model_dump(mode="json") for k, v in pb.pt_routines.items()}),
                pb.directives,
            ),
        )
        conn.commit()


def _strip_html_comments(text: str) -> str:
    """Drop the <!-- ... --> editing notes so they don't reach the model."""
    import re

    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
