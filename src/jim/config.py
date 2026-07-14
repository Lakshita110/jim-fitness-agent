"""Runtime configuration. Secrets come from env vars (see .env.example); the
behavioural constants from PLAN.md §8 live here so they are grep-able and
version-controlled."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# --- PLAN.md §8 constants -------------------------------------------------
MODEL_FAST = "openai/gpt-4o-mini"  # cheap OpenRouter tier — default agent + compose
MODEL_QUALITY = "anthropic/claude-sonnet-4"  # escalation only, on ambiguous state
MAX_TOOL_CALLS = 10  # hard cap per nightly run (reads + gated research + compose ×2)
RESEARCH_ENABLED = True  # gated behind off-heuristic regardless
AUTO_PUSH = False  # propose-only until M5 evals pass
CRON_LOCAL_HOUR = 21

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# --- Guardrail bounds (validate.py) ---------------------------------------
# Hard rules are SAFETY only — they reject a day. Everything else (balance) is
# advice fed back to the coach: an unbalanced week is suboptimal, not dangerous,
# and silently dropping days is worse than letting a skewed plan through.
GARMIN_MAX_STEPS = 50  # Garmin rejects workouts beyond ~50 steps
MAX_SESSION_MIN = 120  # a day's cap; there is deliberately no weekly volume cap

# Balance targets (advisory). Measured across LOADING work only — mobility/PT is
# meant to happen daily and would otherwise swamp the split.
BALANCE_GROUPS = ("legs", "push", "pull", "core", "conditioning")
BALANCE_MAX_SHARE = 0.50  # no single group should own more than half the plan
BALANCE_MIN_SESSIONS = 3  # fewer loading days than this is too short to judge
# Movements that violate current knee/ankle constraints. Lowercase substrings
# matched against exercise names. Extend as PT protocol evolves.
FORBIDDEN_EXERCISES = (
    "depth jump",
    "box jump",
    "jump squat",
    "pistol squat",
    "plyo",
    "sprint",
)
MIN_DAYS_BETWEEN_LEG_SESSIONS = 2


class Settings(BaseSettings):
    """Secrets and per-deploy values, loaded from env / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    garmin_email: str = ""
    garmin_password: str = ""
    # Cached Garmin session tokens as a JSON blob (see scripts/garmin_login.py
    # --export). Required in a container: the token cache at ~/.garminconnect is
    # ephemeral there, and a fresh SSO login would prompt for MFA on a stdin that
    # doesn't exist. When set, this is used instead of the on-disk token store.
    garmin_tokens: str = ""

    notion_token: str = ""
    # Real database ID discovered from the workspace (overridable via env).
    # Notion is READ-ONLY and only supplies the "habits db" (knee+habit log);
    # scheduling context comes from Garmin, so there is no tasks DB.
    notion_knee_log_db_id: str = "b872f62a28604573980e983be6fd3143"

    openrouter_api_key: str = ""
    tavily_api_key: str = ""

    # Vercel sends `Authorization: Bearer $CRON_SECRET` on scheduled invocations.
    # Without it the nightly endpoint is public — anyone could trigger a run.
    cron_secret: str = ""

    database_url: str = ""
    app_timezone: str = "America/New_York"

    # 32-byte, base64-encoded AES-GCM key for encrypting credentials at rest
    # (crypto.py). Generate once, rotate independently of DATABASE_URL.
    credential_encryption_key: str = ""

    # Signing key for the itsdangerous session cookie (auth.py). Long random
    # string; every session becomes invalid if this changes.
    session_secret: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()
