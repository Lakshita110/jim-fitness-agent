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

    # Consecutive steps sharing the same non-null `superset_group` (and the
    # same `sets` count) are wrapped in ONE Garmin repeat block together —
    # "Round 1/3: Wall sit -> Row" as a single grouped unit on the watch —
    # instead of each getting its own separate repeat block. See
    # tools.garmin.build_strength_payload for where this is consumed;
    # Garmin's own structured-workout format has no way to express a
    # multi-exercise repeat block other than nesting several ExecutableStepDTOs
    # inside one RepeatGroupDTO, which is exactly what grouping does here.
    superset_group: int | None = None

    # A true press-to-continue rest step (Garmin's own stepType, not a timed
    # interval standing in for one) — the athlete taps the watch to advance
    # instead of waiting out a fixed timer. When set, `reps`/`duration_sec`/
    # `weight_kg` are ignored and no exercise-taxonomy matching applies,
    # since this isn't a movement. Live-verified: stepTypeId 5 ("rest") +
    # endCondition conditionTypeId 1 ("lap.button") is the real self-paced
    # condition — not something documented anywhere, found by testing
    # against a real account (see tools.garmin._build_entry).
    self_paced_rest: bool = False

    # Garmin's own step role — a distinct stepType, not just descriptive
    # text. Every step defaulted to "interval" before; a real warmup/
    # cooldown/recovery block now shows correctly on the watch instead of
    # being lumped in as a generic interval. Live-verified: stepTypeId
    # 1=warmup, 2=cooldown, 3=interval (the existing default), 4=recovery,
    # 7=other, 8=main — the last two appear in no documentation anywhere,
    # official or reverse-engineered; only found by directly probing ids.
    role: Literal["warmup", "interval", "cooldown", "recovery", "other", "main"] = "interval"

    # Distance-based ending ("run 5km") as an alternative to reps/
    # duration_sec — whichever of reps/duration_sec/distance_m is set wins,
    # in that priority order (matches tools.garmin._build_entry). Meters.
    # Live-verified: conditionTypeId 3, key "distance".
    distance_m: float | None = None

    # Heart rate / power zone target for this step (Garmin's own per-athlete
    # zone numbers, typically 1-5) — e.g. "Zone 2 for 20 min". At most one of
    # target_heart_rate_zone/target_power_zone should be set per step; if
    # both are, heart rate wins. Live-verified: workoutTargetTypeId
    # 4="heart.rate.zone", 2="power.zone", both taking a plain zoneNumber.
    target_heart_rate_zone: int | None = None
    target_power_zone: int | None = None

    # Pace target as a speed range in meters/second — e.g. "5x400m @ 5k
    # pace". Unlike the zone targets above, Garmin's pace target
    # (workoutTargetTypeId 6, "pace.zone") did NOT accept a zoneNumber in
    # testing — only an explicit low/high value pair, live-confirmed to be
    # accepted, but the exact unit convention (assumed m/s, matching
    # distance-in-meters/duration-in-seconds elsewhere in this API) was not
    # independently confirmed against what actually displays on the watch.
    # Flag anything that looks off here first.
    target_pace_min_mps: float | None = None
    target_pace_max_mps: float | None = None

    # A SECOND target alongside the primary one above — e.g. heart-rate zone
    # as the primary target with a cadence range on top. "cadence" is the
    # only secondary kind exposed here (the one live-verified); it's a
    # zoneNumber-style workoutTargetTypeId (3) but — unlike the HR/power
    # zone targets — takes an explicit low/high value pair, same shape as
    # the pace target. Only meaningful alongside target_heart_rate_zone or
    # target_power_zone; ignored on a step with neither set.
    secondary_target_cadence_min: float | None = None
    secondary_target_cadence_max: float | None = None

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

    # A note visible on the workout itself in Garmin, separate from its
    # title — e.g. coaching context ("scaled from last week, +2.5kg").
    # Live-verified working at the whole-workout level; a per-segment
    # description field also exists in Garmin's schema but silently drops
    # whatever's sent to it, so this is intentionally whole-workout only,
    # not per-step. See tools.garmin._wrap_payload for where this lands.
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
