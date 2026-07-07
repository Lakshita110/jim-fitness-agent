"""Typed contracts shared by tools, agent, and guardrail (PLAN.md §7).

Every tool returns one of these compact models — never raw API payloads.
Raw payloads are persisted to the `raw JSON` columns instead so features can
be recomputed without re-fetching."""

from datetime import date, datetime
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
    """Pain / PT / habits from the Notion Knee+Habit log, plus tomorrow's tasks."""

    day: date
    pain_level: int | None = None  # 0-10
    pain_location: str = ""
    pain_notes: str = ""
    pt_done: bool = False
    habits: dict[str, bool] = {}
    day_score: int | None = None
    tomorrow_tasks: list[str] = []


class CheckIn(BaseModel):
    """The athlete's own input for a target day: what they want, how they feel,
    where they'll be. Read from the Notion 'training check-in' DB and folded
    into the compose context. All fields optional — a blank check-in is fine."""

    for_date: date
    note: str = ""  # free text: preferences, active pain, constraints
    focus: str = ""  # upper / lower / full body / conditioning / pt only / rest
    location: str = ""  # gym / home — drives the PT variant
    minutes: int | None = None  # time available
    energy: str = ""  # low / normal / high
    # Notion last_edited_time — lets the morning job detect a check-in written
    # or changed AFTER the nightly proposal, which triggers a same-day re-plan.
    edited_ts: datetime | None = None

    def is_empty(self) -> bool:
        return not any(
            [self.note, self.focus, self.location, self.minutes, self.energy]
        )


class HistoryFeatures(BaseModel):
    """Deterministic features over a trailing window. Pure SQL/Python, no LLM."""

    as_of: date
    window_days: int
    weekly_volume_min: float = 0.0
    muscle_group_balance: dict[str, float] = {}  # group -> fraction of weekly volume
    days_since_legs: int | None = None
    pain_trend: float = 0.0  # slope of pain_level over window; >0 = worsening
    avg_readiness: float | None = None


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
