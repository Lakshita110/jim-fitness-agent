"""Constraints editor API — the small per-athlete record (knee/ankle limits,
safety rules, goals) that replaces the playbook's template library now that
Claude does the reasoning. Free text on purpose: no schema to validate
against, since this is read by a model, not a rotation algorithm."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from jim import db
from jim.web import deps

router = APIRouter()


class ConstraintsBody(BaseModel):
    content: str


@router.get("/api/constraints")
def get_constraints_route(request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    return {"content": db.get_constraints(user.id)}


@router.post("/api/constraints")
def post_constraints_route(body: ConstraintsBody, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    db.set_constraints(user.id, body.content)
    return {"ok": True}
