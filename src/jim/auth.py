"""Email + password auth (soft-baking-kettle plan, Phase 2). Session cookies are
signed/expiring tokens via itsdangerous — no server-side session store, matching
the app's existing DB-free session model.

Scope note: this module only proves "who is this request." It does not thread
user_id through Garmin/Notion/kv/coach — that's Phase 3. There is still only one
real athlete's data in the system; a logged-in user authenticates as themselves,
but the business logic underneath is still global."""

import json

import bcrypt
import psycopg
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from jim.config import settings
from jim.db import connect
from jim.playbook import _load_default_playbook

SESSION_COOKIE_NAME = "jim_session"
SESSION_MAX_AGE = 400 * 24 * 3600  # ~13 months (Chrome caps cookie life at 400d)
_SALT = "jim-session-v2"


class User(BaseModel):
    id: int
    email: str


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_user(email: str, password: str) -> User:
    """Insert a new user plus empty user_credentials rows, seeded with the
    generic default playbook (playbook/defaults/) — not the committed athlete
    YAML, which is this one athlete's own knee-specific content. That real
    content only reaches an account via the one-off backfill script
    (scripts/backfill_users.py)."""
    email = email.strip().lower()
    password_hash = hash_password(password)
    seed = _load_default_playbook()
    try:
        with connect() as conn:
            row = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s)"
                " RETURNING id, email",
                (email, password_hash),
            ).fetchone()
            conn.execute(
                "INSERT INTO user_credentials (user_id) VALUES (%s)", (row["id"],)
            )
            conn.execute(
                "INSERT INTO playbooks (user_id, rotation, workouts, pt_routines, directives)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    row["id"],
                    json.dumps(seed.rotation),
                    json.dumps({k: v.model_dump(mode="json") for k, v in seed.workouts.items()}),
                    json.dumps(
                        {k: v.model_dump(mode="json") for k, v in seed.pt_routines.items()}
                    ),
                    seed.directives,
                ),
            )
            conn.commit()
    except psycopg.errors.UniqueViolation as e:
        raise ValueError("an account with this email already exists") from e
    return User(id=row["id"], email=row["email"])


def get_user_by_email(email: str) -> User | None:
    email = email.strip().lower()
    row = _get_user_row_by_email(email)
    return User(id=row["id"], email=row["email"]) if row else None


def first_user_id() -> int | None:
    """The lowest user id in the system. Used by one-off local scripts
    (scripts/backfill.py, exercise_map.py, m1_roundtrip.py) that operate on
    "whoever's account is here" during development — not by app.py or the
    nightly job, which resolve a real caller/fan out over every user."""
    with connect() as conn:
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def get_user_by_id(user_id: int) -> User | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return User(id=row["id"], email=row["email"]) if row else None


def _get_user_row_by_email(email: str) -> dict | None:
    email = email.strip().lower()
    with connect() as conn:
        return conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()


def authenticate(email: str, password: str) -> User | None:
    """Verify credentials; returns None on any failure (unknown email or bad
    password) so callers can give a generic, non-enumerating 401."""
    row = _get_user_row_by_email(email)
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return User(id=row["id"], email=row["email"])


def create_session_token(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def verify_session_token(token: str) -> int | None:
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    uid = data.get("uid")
    return int(uid) if isinstance(uid, int) else None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings().session_secret, salt=_SALT)
