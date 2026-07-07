"""Playbook — the durable, human-editable memory layer (see playbook/).

Three files, loaded into the agent's context every night:
- base_workouts.yaml : the A/B/C strength rotation (references Garmin IDs)
- pt_routines.yaml   : home + gym PT for non-lifting days
- directives.md      : standing instructions the user edits in plain English

This is the "give instructions to the agent" surface. Editing a file changes
the next run — no code change, no DB write. The loader validates structure and
renders a compact text block for the compose prompt; `garmin_workout_id` lets
the loop schedule an existing Garmin workout directly instead of rebuilding it."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel

log = logging.getLogger(__name__)

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent.parent / "playbook"


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


def load_playbook(directory: Path = PLAYBOOK_DIR) -> Playbook:
    base = yaml.safe_load((directory / "base_workouts.yaml").read_text()) or {}
    pt = yaml.safe_load((directory / "pt_routines.yaml").read_text()) or {}
    directives_path = directory / "directives.md"
    directives = directives_path.read_text() if directives_path.exists() else ""

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


def _strip_html_comments(text: str) -> str:
    """Drop the <!-- ... --> editing notes so they don't reach the model."""
    import re

    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
