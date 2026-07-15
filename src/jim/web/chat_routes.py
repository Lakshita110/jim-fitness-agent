"""The chat API and the /chat page itself."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from jim import coach
from jim.web import deps
from jim.web.templates import CHAT_PAGE

router = APIRouter()


class KeyOnly(BaseModel):
    pass


class ChatMessage(BaseModel):
    text: str
    scope_date: str | None = None  # ISO date: edit only this day


class PushDay(BaseModel):
    date: str  # ISO date of the draft day to push/update


@router.post("/chat/message")
def chat_message(msg: ChatMessage, request: Request) -> dict:
    user = deps._require_user(request)
    if not msg.text.strip():
        raise HTTPException(status_code=400, detail="empty message")
    deps._ready()
    return coach.converse(msg.text.strip(), user.id, scope_date=msg.scope_date)


@router.post("/chat/approve")
def chat_approve(body: KeyOnly, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    summary = coach.approve(user.id)
    state = coach.current_state(user.id)
    return {"summary": summary, "draft": state["draft"],
            "push_status": state["push_status"]}


@router.post("/chat/push-day")
def chat_push_day(body: PushDay, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    return coach.push_day(body.date, user.id)


@router.post("/chat/clear")
def chat_clear(body: KeyOnly, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    coach.clear(user.id)
    return {"ok": True}


@router.get("/chat/state")
def chat_state(request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    return coach.current_state(user.id)


@router.get("/chat")
def chat_page(request: Request) -> Response:
    """Cookie-only now — no ?key= sign-in. An unauthenticated visit bounces to
    /login instead of erroring, since this is the URL people bookmark/add to
    their home screen."""
    if deps._current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(CHAT_PAGE)
