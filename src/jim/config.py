"""Runtime configuration. Secrets come from env vars (see .env.example); the
behavioural constants used across the codebase live here so they are
grep-able and version-controlled."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

MODEL_FAST = "openai/gpt-4o-mini"  # cheap OpenRouter tier — research.py + exercise_match.py
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
