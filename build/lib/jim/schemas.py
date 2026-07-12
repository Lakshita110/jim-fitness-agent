"""Typed contracts shared by tools, agent, and guardrail (PLAN.md §7).

Every tool returns one of these compact models — never raw API payloads.
Raw payloads are persisted to the `raw JSON` columns instead so features can
be recomputed without re-fetching."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

SessionKind = Literal["strength", "conditioning", "mobility", "rest"]


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


class NotionDay(BaseModel):
    """Pain / PT / habits from the Notion Knee+Habit log."""

    day: date
    pain_level: int | None = None  # 0-10
    pain_location: str = ""
    pain_notes: str = ""
    pt_done: bool = False
    habits: dict[str, bool] = {}
    day_score: float | None = None  # Notion formula returns a fraction (e.g. 0.5)


class HistoryFeatures(BaseModel):
    """Deterministic features over a trailing window. Pure SQL/Python, no LLM."""

    as_of: date
    window_days: int
    weekly_volume_min: float = 0.0
    muscle_group_balance: dict[str, float] = {}  # group -> fraction of weekly volume
    days_since_legs: int | None = None
    pain_trend: float = 0.0  # slope of pain_level over window; >0 = worsening
    # Most recent dated pain notes, newest first — the words behind the trend,
    # so a recurring complaint ("wrists still poor") is visible, not just a slope.
    recent_pain_notes: list[str] = []
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


class StructuredSession(BaseModel):
    """The one truly generative output: tomorrow's session as Garmin-ready JSON."""

    for_date: date
    kind: SessionKind
    title: str
    steps: list[ExerciseStep] = []
    est_duration_min: float = Field(default=0.0, ge=0)
    rationale_summary: str = ""
    # When the agent selects a base template unchanged, it returns that
    # template's Garmin workout ID so the loop schedules the existing workout
    # (preserving loaded weights) instead of rebuilding it from `steps`.
    garmin_workout_id: str | None = None
    template_key: str | None = None


class ResearchHit(BaseModel):
    source: str
    title: str
    snippet: str
    score: float = 0.0


class WorkoutRef(BaseModel):
    workout_id: str
    provider: Literal["garmin"] = "garmin"


class ValidationResult(BaseModel):
    ok: bool
    violations: list[str] = []
