"""Runtime configuration. Secrets come from env vars (see .env.example); the
behavioural constants from PLAN.md §8 live here so they are grep-able and
version-controlled."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# --- PLAN.md §8 constants -------------------------------------------------
MODEL_FAST = "openai/gpt-4o-mini"  # cheap OpenRouter tier — default agent + compose
MODEL_QUALITY = "anthropic/claude-sonnet-4"  # escalation only, on ambiguous state
MAX_TOOL_CALLS = 8  # hard cap per nightly run
RESEARCH_ENABLED = True  # gated behind off-heuristic regardless
AUTO_PUSH = False  # propose-only until M5 evals pass
CRON_LOCAL_HOUR = 21

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# --- Guardrail bounds (validate.py) ---------------------------------------
GARMIN_MAX_STEPS = 50  # Garmin rejects workouts beyond ~50 steps
MAX_WEEKLY_VOLUME_MIN = 600  # minutes/week hard ceiling
MAX_SESSION_MIN = 120
MAX_LOAD_PROGRESSION = 0.10  # max +10% week-over-week volume jump
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

    notion_token: str = ""
    # Real database IDs discovered from the workspace (overridable via env):
    # "habits db", "tasks ", and the "training proposals" DB under the habits page.
    notion_knee_log_db_id: str = "b872f62a28604573980e983be6fd3143"
    notion_tasks_db_id: str = "6843311f33194f40b65ea7e7c0f47436"
    notion_proposal_db_id: str = "67d2cfc3c75442c4b373736ad38b1cda"

    openrouter_api_key: str = ""
    tavily_api_key: str = ""

    database_url: str = ""
    app_timezone: str = "America/New_York"


@lru_cache
def settings() -> Settings:
    return Settings()
