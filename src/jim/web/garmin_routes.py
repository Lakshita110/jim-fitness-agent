"""Garmin web onboarding (soft-baking-kettle plan Phase 4).

The installed garminconnect (>=0.2.19, verified by reading the installed
package's client.py) offers a clean two-step resumable MFA flow:
Garmin(email, password, return_on_mfa=True).login() returns ("needs_mfa", None)
instead of blocking on stdin, and a later g.resume_login(client_state, mfa_code)
completes it against the SAME in-memory client object (resume_login's
client_state argument is accepted but unused internally — MFA state lives on
the Garmin/Client instance itself). So the in-progress client has to be held
server-side between the two requests; it is NEVER persisted to the DB, only
kept in this process-local dict with a short TTL."""

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from jim.web import deps
from jim.web.templates import GARMIN_PAGE

router = APIRouter()

_pending_garmin_logins: dict[int, dict] = {}
_GARMIN_MFA_TTL_SEC = 300  # long enough to fetch a code from the Garmin Connect app


class GarminConnectBody(BaseModel):
    garmin_email: str
    garmin_password: str


class GarminMfaBody(BaseModel):
    mfa_code: str


def _save_garmin_login(user_id: int, email: str, password: str, g: object) -> None:
    from jim import db

    blob = g.client.dumps()  # same mechanism scripts/garmin_login.py --export uses
    db.save_user_credentials(
        user_id, garmin_email=email, garmin_password=password, garmin_tokens=blob
    )


@router.get("/settings/garmin")
def garmin_settings_page(request: Request) -> Response:
    if deps._current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(GARMIN_PAGE)


@router.get("/api/garmin/status")
def garmin_status(request: Request) -> dict:
    """Whether this user already has a working Garmin connection, so the
    settings page can show 'Connected as X' instead of always presenting a
    blank login form — landing here after connecting looked exactly like the
    connection never took, even though it had."""
    from jim import db

    user = deps._require_user(request)
    creds = db.get_user_credentials(user.id)
    connected = bool(creds and (creds.get("garmin_tokens") or creds.get("garmin_password")))
    return {"connected": connected, "garmin_email": creds.get("garmin_email") if creds else None}


@router.post("/settings/garmin/connect")
def garmin_connect(body: GarminConnectBody, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    g = Garmin(body.garmin_email, body.garmin_password, return_on_mfa=True)
    try:
        mfa_status, _legacy_token = g.login()
    except GarminConnectAuthenticationError as e:
        raise HTTPException(
            status_code=400,
            detail="Garmin login failed — check your email and password.",
        ) from e
    except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if mfa_status == "needs_mfa":
        _pending_garmin_logins[user.id] = {
            "client": g,
            "email": body.garmin_email,
            "password": body.garmin_password,
            "ts": time.time(),
        }
        return {"mfa_required": True}

    _save_garmin_login(user.id, body.garmin_email, body.garmin_password, g)
    return {"ok": True}


@router.post("/settings/garmin/mfa")
def garmin_mfa(body: GarminMfaBody, request: Request) -> dict:
    user = deps._require_user(request)
    deps._ready()
    pending = _pending_garmin_logins.get(user.id)
    if pending is None or time.time() - pending["ts"] > _GARMIN_MFA_TTL_SEC:
        _pending_garmin_logins.pop(user.id, None)
        raise HTTPException(
            status_code=400,
            detail="No pending Garmin login (or it expired) — start again.",
        )
    g = pending["client"]
    try:
        g.resume_login({}, body.mfa_code)
    except Exception as e:  # resume_login doesn't guarantee a typed auth error
        # Wrong code — leave the pending login in place so the user can retry
        # without re-entering email/password.
        raise HTTPException(
            status_code=400, detail="Invalid MFA code — try again."
        ) from e

    _pending_garmin_logins.pop(user.id, None)
    _save_garmin_login(user.id, pending["email"], pending["password"], g)
    return {"ok": True}
