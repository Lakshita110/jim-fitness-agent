"""Email + password signup/login/logout — sets the signed session cookie
(auth.py) that every other route group's _require_user reads."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from jim import auth
from jim.web import deps

router = APIRouter()


class SignupBody(BaseModel):
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


def _set_session_cookie(response: Response, user_id: int, secure: bool) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        auth.create_session_token(user_id),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,   # JS can't read it, so an XSS can't exfiltrate the session
        secure=secure,   # https only in prod; must be off on plain-http localhost
        samesite="lax",
    )


@router.post("/auth/signup")
def auth_signup(body: SignupBody, request: Request, response: Response) -> dict:
    deps._ready()
    try:
        user = auth.create_user(body.email, body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _set_session_cookie(response, user.id, secure=request.url.scheme == "https")
    return {"ok": True}


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response) -> dict:
    deps._ready()
    user = auth.authenticate(body.email, body.password)
    if user is None:
        # Generic on purpose: same message whether the email doesn't exist or
        # the password is wrong, so the response can't be used to enumerate
        # accounts.
        raise HTTPException(status_code=401, detail="invalid email or password")
    _set_session_cookie(response, user.id, secure=request.url.scheme == "https")
    return {"ok": True}


@router.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return {"ok": True}
