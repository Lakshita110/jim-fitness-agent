"""Cross-cutting request helpers used by every route group (auth, garmin
onboarding, constraints) — kept separate from any one group since none of
them own these."""

from fastapi import HTTPException, Request

from jim import auth
from jim.auth import User


def _ready() -> None:
    """Schema is present before we touch the DB. No-op after the first call."""
    from jim.db import ensure_migrated

    ensure_migrated()


def _bearer_token(request: Request) -> str:
    """The MCP server (and any other non-browser client) has no cookie jar, so
    it authenticates with the same signed token via `Authorization: Bearer
    <token>` instead — same verification, different transport."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value if scheme.lower() == "bearer" else ""


def _current_user(request: Request) -> User | None:
    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "") or _bearer_token(request)
    if not token:
        return None
    user_id = auth.verify_session_token(token)
    if user_id is None:
        return None
    _ready()
    return auth.get_user_by_id(user_id)


def _require_user(request: Request) -> User:
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="not authenticated")
    return user
