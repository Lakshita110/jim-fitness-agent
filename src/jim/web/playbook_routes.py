"""Playbook editor API (soft-baking-kettle plan Phase 4) and importing real
Garmin workouts into it.

Validated-JSON-textarea MVP (decided in the plan over a structured form).
All-or-nothing validation: a partially-applied playbook edit is worse than a
rejected one with a clear error, so a bad submission never touches storage."""

import json

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from jim.playbook import Playbook, load_playbook, save_playbook
from jim.web import deps

router = APIRouter()


class PlaybookBody(BaseModel):
    raw: str


@router.get("/api/playbook")
def get_playbook(request: Request) -> Response:
    user = deps._require_user(request)
    deps._ready()
    pb = load_playbook(user.id)
    return Response(
        json.dumps(pb.model_dump(mode="json"), indent=2),
        media_type="application/json",
    )


@router.post("/api/playbook")
def post_playbook(body: PlaybookBody, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    try:
        parsed = json.loads(body.raw)
        pb = Playbook.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    save_playbook(user.id, pb)
    return {"ok": True}


class GarminWorkoutImportBody(BaseModel):
    workout_id: str
    key: str
    label: str | None = None
    target: str = "workouts"  # "workouts" (base rotation) or "pt_routines"
    add_to_rotation: bool = False


@router.get("/api/garmin/workouts")
def list_garmin_workouts_route(request: Request) -> dict:
    """The athlete's existing Garmin workout library, so the playbook editor
    can offer 'import this one' instead of only hand-typed YAML/JSON.

    Enriched with two flags the picker uses to declutter itself: `jim_created`
    (this is a one-off adaptation Jim itself built for a single day — see the
    "jim_created_workouts" kv entry written by coach._push_one, not a reusable
    template) and `already_in_playbook` (it's already referenced by a template,
    so re-importing it would be pointless)."""
    user = deps._require_user(request)
    deps._ready()
    from jim.db import kv_get
    from jim.tools.garmin import list_garmin_workouts

    try:
        workouts = list_garmin_workouts(user.id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    created = kv_get(user.id, "jim_created_workouts") or {}
    by_workout_id = {v["workout_id"]: (fd, v) for fd, v in created.items()}
    pb = load_playbook(user.id)
    for w in workouts:
        hit = by_workout_id.get(w["workout_id"])
        w["jim_created"] = hit is not None
        w["for_date"] = hit[0] if hit else None
        w["template_key"] = hit[1]["template_key"] if hit else None
        w["already_in_playbook"] = pb.by_workout_id(w["workout_id"]) is not None
    return {"workouts": workouts}


@router.post("/api/garmin/workouts/import")
def import_garmin_workout(body: GarminWorkoutImportBody, request: Request) -> dict:
    """Pull one real Garmin workout in by id and save it into the playbook
    under `body.key`, so it can be scheduled by ID like any other template
    (see playbook.use_existing_workout)."""
    user = deps._require_user(request)
    deps._ready()
    from jim.playbook import promote_garmin_workout

    try:
        promote_garmin_workout(
            user.id, body.workout_id, body.key, body.target,
            label=body.label, add_to_rotation=body.add_to_rotation,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    pb = load_playbook(user.id)
    return {"ok": True, "playbook": pb.model_dump(mode="json")}
