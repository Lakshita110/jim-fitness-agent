"""Typed contracts shared by the Garmin tools and MCP server.

Every tool returns one of these compact models — never raw API payloads.
Raw payloads are persisted to the `raw JSON` columns instead so features can
be recomputed without re-fetching."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# The original four buckets ("strength", "conditioning", "mobility", "rest")
# plus the real, more specific Garmin sport types mcp_server.create_or_update_
# workout accepts, so a workout doesn't have to be shoehorned into
# "conditioning" to be created — see tools.garmin.SPORT_TYPES for what each
# maps to on Garmin's side.
SessionKind = Literal[
    "strength", "conditioning", "mobility", "rest",
    "running", "cycling", "swimming", "walking", "hiking", "yoga", "pilates",
    "hiit", "rucking", "other",
]


class ActivitySummary(BaseModel):
    activity_id: str
    type: str
    duration_min: float
    training_load: float | None = None
    notes: str = ""


class GarminToday(BaseModel):
    """Compact end-of-day Garmin summary (activities + recovery)."""

    day: date
    activities: list[ActivitySummary] = []
    hrv: float | None = None
    sleep_hours: float | None = None
    body_battery: int | None = None
    readiness: int | None = None
    resting_hr: int | None = None


class HistoryFeatures(BaseModel):
    """Deterministic features over a trailing window. Pure SQL/Python, no LLM."""

    as_of: date
    window_days: int
    weekly_volume_min: float = 0.0
    muscle_group_balance: dict[str, float] = {}  # group -> fraction of weekly volume
    days_since_legs: int | None = None
    avg_readiness: float | None = None


class ReadinessRead(BaseModel):
    """Load + recovery distilled into a single planning verdict.

    Garmin Connect already charts the raw numbers; this exists only to turn
    them into a decision — how hard tomorrow should be — for the coach and a
    one-glance UI badge. `status` drives both."""

    as_of: date
    acute_load: float = 0.0  # trailing 7-day workload (training load or minutes)
    chronic_load: float = 0.0  # trailing 28-day workload / 4 (avg week)
    acwr: float | None = None  # acute:chronic ratio; sweet spot ~0.8-1.3
    basis: Literal["load", "minutes", "none"] = "none"  # what the ratio is built from
    readiness: int | None = None  # Garmin Training Readiness (0-100)
    body_battery: int | None = None
    hrv: float | None = None
    sleep_hours: float | None = None
    status: Literal["push", "steady", "ease", "rest"] = "steady"
    headline: str = ""  # glanceable one-liner for the UI badge
    detail: str = ""  # short numeric reason for the coach's context


class ExerciseStep(BaseModel):
    exercise: str
    sets: int = 1
    reps: int | None = None
    duration_sec: int | None = None
    weight_kg: float | None = None
    notes: str = ""

    # The model routinely sends explicit `null` for `sets` on a duration-only
    # step (e.g. a plank) instead of omitting the key — a bare `int` field
    # rejects that outright, and _parse_draft drops the WHOLE day on any one
    # step's validation error. Caught testing against a real account: several
    # otherwise-fine days vanished from the plan with only a log line to show
    # for it. Coerce to the default rather than let one field's null nuke a
    # session the model got everything else right on.
    @field_validator("sets", mode="before")
    @classmethod
    def _sets_default_on_null(cls, v: int | None) -> int:
        return 1 if v is None else v


class StructuredSession(BaseModel):
    """The one truly generative output: tomorrow's session as Garmin-ready JSON."""

    for_date: date
    kind: SessionKind
    title: str
    steps: list[ExerciseStep] = []
    est_duration_min: float = Field(default=0.0, ge=0)
    rationale_summary: str = ""

    # Same failure mode as ExerciseStep.sets — a rest day with nothing to
    # estimate sometimes comes back with `"est_duration_min": null` rather
    # than 0, which would otherwise drop the whole day.
    @field_validator("est_duration_min", mode="before")
    @classmethod
    def _duration_default_on_null(cls, v: float | None) -> float:
        return 0.0 if v is None else v

    # When the agent selects a base template unchanged, it returns that
    # template's Garmin workout ID so the loop schedules the existing workout
    # (preserving loaded weights) instead of rebuilding it from `steps`.
    garmin_workout_id: str | None = None

    # Caught testing live: the model sometimes emits the ID as a JSON number
    # (it IS numeric-looking) rather than a string. Pydantic's `str` field
    # does not coerce int -> str by default, so this silently dropped the
    # whole day too — same failure class as the two null-coercions above,
    # different field.
    @field_validator("garmin_workout_id", mode="before")
    @classmethod
    def _garmin_id_as_string(cls, v: str | int | None) -> str | None:
        return str(v) if isinstance(v, int) else v
    template_key: str | None = None


class ResearchHit(BaseModel):
    source: str
    title: str
    snippet: str
    score: float = 0.0


class WorkoutRef(BaseModel):
    workout_id: str
    provider: Literal["garmin"] = "garmin"
