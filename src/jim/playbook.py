"""Playbook — the durable, human-editable memory layer (see playbook/).

Two files, loaded into the agent's context every night:
- base_workouts.yaml : one flat workout library (strength + PT), plus the
                        `rotation` order drawn from it (references Garmin IDs)
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
    directives: str = ""

    def template(self, key: str) -> WorkoutTemplate | None:
        return self.workouts.get(key)

    def by_workout_id(self, workout_id: str) -> WorkoutTemplate | None:
        """Reverse lookup — the model reliably echoes the Garmin ID even when it
        forgets (or invents) the template_key."""
        for wt in self.workouts.values():
            if wt.garmin_workout_id == workout_id:
                return wt
        return None

    def rotation_from(self, last_key: str | None) -> list[str]:
        """The rotation re-ordered to start at whatever follows `last_key`.

        Multi-day planning needs the whole continuing order, not just the next
        key — the coach lays out a week in one turn, so it has to know that
        after B comes C then A then B again. An unknown or missing `last_key`
        starts at the top."""
        if not self.rotation:
            return []
        if last_key not in self.rotation:
            return list(self.rotation)
        i = self.rotation.index(last_key) + 1
        return self.rotation[i:] + self.rotation[:i]

    def to_prompt(self) -> str:
        """Compact rendering for the compose prompt — the model sees names,
        doses, and tags, not raw YAML."""
        lines = ["## Rotation (schedule the Garmin ID as-is on lifting days)"]
        lines.append(f"Rotation order: {' → '.join(self.rotation)}")
        for key in self.rotation:
            wt = self.workouts.get(key)
            if wt:
                lines.append(_render_template(wt))
        other = [wt for key, wt in self.workouts.items() if key not in self.rotation]
        if other:
            lines.append("\n## Other workouts (not in rotation — non-lifting days, PT, etc.)")
            for wt in other:
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
    landed on the watch instead.

    The ID must resolve against THIS playbook even when steps is empty — caught
    live testing against a real account, where a model invented a whole template
    ("PT Day · Gym", a plausible-looking 10-digit ID) that wasn't in the
    athlete's playbook at all. With no resolution check here, that would have
    scheduled an unverified ID straight onto the watch: either a loud Garmin
    error, or — worse — a real but unrelated workout that happened to share
    that ID, scheduled with no record of why."""
    if not session.garmin_workout_id:
        return False

    wt = playbook.template(session.template_key or "") or playbook.by_workout_id(
        session.garmin_workout_id
    )
    if wt is None:
        return False  # unresolvable ID — never trust it, empty steps or not
    if not session.steps:
        return True
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
    # The key is shown because it's the handle the playbook-writing tools take
    # (save_playbook_workout / set_playbook_rotation) — without it the model can
    # only name templates it can't actually address.
    head = f"\n### {wt.label} (key={wt.key}"
    if wt.garmin_workout_id:
        head += f", garmin_workout_id={wt.garmin_workout_id}"
    head += ")"
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
    directives_path = directory / "directives.md"
    directives = directives_path.read_text("utf-8") if directives_path.exists() else ""

    workouts = {
        key: WorkoutTemplate(key=key, **spec)
        for key, spec in (base.get("workouts") or {}).items()
    }
    return Playbook(
        rotation=base.get("rotation", []),
        workouts=workouts,
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
            "SELECT rotation, workouts, directives FROM playbooks"
            " WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if row is None:
        return Playbook()  # safety net; a row should exist post-signup
    # A saved template's JSON already carries its own "key" (model_dump includes
    # every field) — override rather than pass both, or WorkoutTemplate(key=k, **v)
    # raises "multiple values for keyword argument 'key'".
    workouts = {k: WorkoutTemplate(**{**v, "key": k}) for k, v in row["workouts"].items()}
    return Playbook(
        rotation=row["rotation"], workouts=workouts,
        directives=row["directives"],
    )


def save_playbook(user_id: int, pb: Playbook) -> None:
    """Upsert `pb` for `user_id`. Phase 4's /api/playbook POST route calls this."""
    from jim.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO playbooks (user_id, rotation, workouts, directives,"
            " updated_ts) VALUES (%s, %s, %s, %s, now())"
            " ON CONFLICT (user_id) DO UPDATE SET rotation = EXCLUDED.rotation,"
            " workouts = EXCLUDED.workouts,"
            " directives = EXCLUDED.directives, updated_ts = now()",
            (
                user_id,
                json.dumps(pb.rotation),
                json.dumps({k: v.model_dump(mode="json") for k, v in pb.workouts.items()}),
                pb.directives,
            ),
        )
        conn.commit()


def promote_garmin_workout(
    user_id: int, workout_id: str, key: str,
    label: str | None = None, add_to_rotation: bool = False,
) -> WorkoutTemplate:
    """Pull a real Garmin workout in by id and save it into the playbook under
    `key` — the one path (besides hand-editing raw JSON) that turns something
    on the athlete's Garmin account into a durable default. Used by both the
    /api/garmin/workouts/import route (human-driven, from the Playbook panel)
    and coach.py's promote_workout_to_playbook tool (model-callable, from chat)
    so the two surfaces can't drift apart.

    Also clears the workout out of "jim_created_workouts" kv tracking if it's
    there — once promoted it's a real template, not a one-off cleanup
    candidate (see coach._push_one and jobs.nightly.cleanup_stale_adaptations)."""
    from jim.db import kv_get, kv_set
    from jim.tools.garmin import get_garmin_workout_detail, parse_workout_to_template

    raw = get_garmin_workout_detail(user_id, workout_id)
    template = parse_workout_to_template(key, raw)
    if label:
        template.label = label

    pb = load_playbook(user_id)
    pb.workouts[key] = template
    if add_to_rotation and key not in pb.rotation:
        pb.rotation.append(key)
    save_playbook(user_id, pb)

    created = kv_get(user_id, "jim_created_workouts") or {}
    stale = [fd for fd, v in created.items() if v["workout_id"] == workout_id]
    for fd in stale:
        del created[fd]
    if stale:
        kv_set(user_id, "jim_created_workouts", created)

    return template


def save_workout_template(
    user_id: int, key: str, *,
    label: str | None = None,
    sport: str | None = None,
    warmup: list[Exercise] | None = None,
    blocks: list[Block] | None = None,
    equipment: list[str] | None = None,
) -> WorkoutTemplate:
    """Upsert a playbook template — the chat-side counterpart to
    promote_garmin_workout, which can only pull in a workout that already
    exists on Garmin. This one both creates templates from scratch and edits
    existing ones, so a whole new rotation can be authored in conversation.

    Creating requires `label` and `sport`; editing applies only the fields
    supplied and leaves the rest alone."""
    pb = load_playbook(user_id)
    wt = pb.workouts.get(key)

    if wt is None:
        missing = [n for n, v in (("label", label), ("sport", sport)) if not v]
        if missing:
            raise RuntimeError(
                f"creating workout {key!r} needs {' and '.join(missing)}"
            )
        wt = WorkoutTemplate(key=key, label=label or "", sport=sport or "")
        pb.workouts[key] = wt
    else:
        if label is not None:
            wt.label = label
        if sport is not None:
            wt.sport = sport

    if equipment is not None:
        wt.equipment = equipment
    if warmup is not None:
        wt.warmup = warmup
    if blocks is not None:
        wt.blocks = blocks

    # Only a content change invalidates the Garmin link. There's no Garmin
    # "update workout" API (tools/garmin.py has create/delete/schedule only),
    # so a template whose steps changed must be rebuilt fresh next time it's
    # scheduled rather than reusing the old object with stale steps. A
    # rename, though, must NOT throw away the workout — and with it the
    # weights the athlete has loaded onto it.
    if warmup is not None or blocks is not None:
        wt.garmin_workout_id = None

    save_playbook(user_id, pb)
    return wt


def set_rotation(user_id: int, keys: list[str]) -> list[str]:
    """Replace the rotation order outright, so the coach can restructure a
    program (not just append to it, which is all promote_garmin_workout can do).

    Every key must already exist in `workouts` — a rotation pointing at a
    template that isn't there would schedule nothing. Repeats are allowed: a
    rotation is an order, not a set, so [a, b, a, c] is legitimate."""
    pb = load_playbook(user_id)
    unknown = [k for k in keys if k not in pb.workouts]
    if unknown:
        raise RuntimeError(
            "no playbook workout with key " + ", ".join(repr(k) for k in unknown)
        )
    pb.rotation = list(keys)
    save_playbook(user_id, pb)
    return pb.rotation


def _strip_html_comments(text: str) -> str:
    """Drop the <!-- ... --> editing notes so they don't reach the model."""
    import re

    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
