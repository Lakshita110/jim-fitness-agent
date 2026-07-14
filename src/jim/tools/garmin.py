"""Garmin tools: read today's state, create + schedule structured workouts.

Auth is mobile-SSO via `python-garminconnect`; tokens cache at ~/.garminconnect
and MFA may be prompted on first/expired login. Never hardcode credentials.

The write path is the workout API (JSON) — FIT structured-workout upload is
rejected (406). The accepted payload shape is verified and documented in
docs/garmin_strength.md; read it before changing `build_strength_payload`, as
each rule there cost a live 400 or a silently dropped field."""

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jim.config import settings
from jim.schemas import ActivitySummary, GarminToday, StructuredSession, WorkoutRef

if TYPE_CHECKING:
    from jim.playbook import WorkoutTemplate
    from jim.tools.exercise_match import Resolver

log = logging.getLogger(__name__)

_client: Any = None


TOKEN_STORE = "~/.garminconnect"
# garminconnect's login() switches on length: >512 chars = token data, else a
# filesystem path. Anything shorter than this is a mangled blob, not a session.
MIN_TOKEN_BLOB_CHARS = 512


def client() -> Any:
    """Lazily authenticated Garmin client (cached tokens, re-login on expiry).

    Two token sources, in order:
    1. GARMIN_TOKENS — a session blob (scripts/garmin_login.py --export). This is
       what deployed containers use: their filesystem is ephemeral and a fresh
       SSO login would block on an MFA prompt with no stdin to answer it.
       `login()` treats a string >512 chars as token data rather than a path.
    2. TOKEN_STORE (~/.garminconnect) — the local dev path; a full SSO login
       happens on first use (MFA prompt on stdin) and caches tokens there.
    """
    global _client
    if _client is None:
        from garminconnect import Garmin

        cfg = settings()
        garmin = Garmin(cfg.garmin_email, cfg.garmin_password)
        tokens = (cfg.garmin_tokens or "").strip()
        if tokens:
            # login() only treats the string as token data above 512 chars —
            # below that it silently falls back to reading it as a PATH, which
            # fails in a confusing way. Catch a truncated/mangled blob here.
            if len(tokens) <= MIN_TOKEN_BLOB_CHARS:
                raise RuntimeError(
                    f"GARMIN_TOKENS is only {len(tokens)} chars; a real session blob is"
                    f" >{MIN_TOKEN_BLOB_CHARS}. It was likely truncated when pasted."
                    " Re-run: python scripts/garmin_login.py --export"
                )
            log.info("garmin: authenticating from GARMIN_TOKENS blob")
            garmin.login(tokens)
        else:
            log.info("garmin: authenticating from token store %s", TOKEN_STORE)
            garmin.login(TOKEN_STORE)
        _client = garmin
    return _client


def body_battery_recovered(stats: dict) -> int | None:
    """How charged the athlete woke up — the recovery read worth planning from.

    Body battery drains all day, so "bodyBatteryMostRecentValue" is whatever was
    left at the last sync (typically single digits by bedtime). Reading that as
    recovery put ~84% of days under the "poor recovery" threshold and had the
    coach prescribing rest almost every day. Prefer the value at wake, then the
    day's peak, and only fall back to the most-recent reading."""
    for key in ("bodyBatteryAtWakeTime", "bodyBatteryHighestValue",
                "bodyBatteryMostRecentValue"):
        value = stats.get(key)
        if value is not None:
            return value
    return None


def get_garmin_today(day: date) -> GarminToday:
    """Activities + recovery for `day`. Computation done here; returns summary."""
    api = client()
    iso = day.isoformat()

    activities = []
    for raw in api.get_activities_by_date(iso, iso) or []:
        activities.append(
            ActivitySummary(
                activity_id=str(raw.get("activityId", "")),
                type=str(raw.get("activityType", {}).get("typeKey", "unknown")),
                duration_min=round(float(raw.get("duration") or 0) / 60, 1),
                training_load=raw.get("activityTrainingLoad"),
            )
        )

    stats = api.get_stats(iso) or {}
    sleep = (api.get_sleep_data(iso) or {}).get("dailySleepDTO") or {}
    hrv = ((api.get_hrv_data(iso) or {}).get("hrvSummary") or {}).get("lastNightAvg")

    sleep_sec = sleep.get("sleepTimeSeconds")
    return GarminToday(
        day=day,
        activities=activities,
        hrv=hrv,
        sleep_hours=round(sleep_sec / 3600, 1) if sleep_sec else None,
        body_battery=body_battery_recovered(stats),
        readiness=stats.get("trainingReadinessScore"),
        resting_hr=stats.get("restingHeartRate"),
    )


# --- matching a movement to Garmin's exercise taxonomy ------------------------
#
# Garmin's taxonomy is a closed enum: `category` must be one of its ~47 categories
# (free text fails with "Invalid category") and `exerciseName` must be one of that
# category's exercises. Get no match and the step lands on the watch as a bare
# description — a note with no exercise, no animation, and no set logging. That is
# the failure this module exists to avoid, so every movement is matched to the
# CLOSEST thing Garmin actually has rather than left unmapped.
#
# The full library (1500+ exercises) is vendored at data/garmin_exercises.json —
# see scripts/refresh_garmin_exercises.py. Matching is:
#   1. EXERCISE_OVERRIDES — where the nearest name is the wrong movement, or the
#      movement simply isn't in the library. Hand-verified; first match wins.
#   2. nearest name in the library, by token overlap.
#   3. nothing above the confidence floor -> description only.

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Movements where the library's nearest name would be wrong (or missing). Mostly
# knee/ankle PT, which Garmin's strength-oriented taxonomy barely covers. Needles
# are matched against the normalized name, so "single leg bridge" also catches
# "Single-Leg Bridge".
EXERCISE_OVERRIDES: tuple[tuple[str, str, str | None], ...] = (
    # knee: the wall-position isometrics all map to the one enum Garmin has for
    # them; the library would offer WEIGHTED_WALL_SQUAT or a plain SQUAT instead.
    ("wall sit", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    ("wall squat", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    ("spanish squat", "SQUAT", "BODY_WEIGHT_WALL_SQUAT"),
    ("step down", "SQUAT", "STEP_UP"),  # eccentric emphasis noted in description
    ("terminal knee extension", "BANDED_EXERCISES", "LEG_EXTENSION"),
    ("short arc quad", "CRUNCH", "LEG_EXTENSIONS"),  # account precedent for iso holds
    ("quad set", "CRUNCH", "LEG_EXTENSIONS"),
    # hip: "single leg …" otherwise drags in whatever single-leg move shares the
    # most words, which is rarely the right one.
    ("single leg bridge", "HIP_RAISE", "SINGLE_LEG_HIP_RAISE"),
    ("single leg circles", "HIP_STABILITY", "HIP_CIRCLES"),  # no single-leg variant exists
    ("single leg reach", "HIP_STABILITY", None),
    ("hip controlled articular", "HIP_STABILITY", "HIP_CIRCLES"),
    ("dead bug", "HIP_STABILITY", "DEAD_BUG"),  # over BANDED_EXERCISES/DEADBUG
    # ankle/calf
    ("eccentric calf raise", "CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
    ("single leg calf raise", "CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
    ("eversion", "CALF_RAISE", None),  # Garmin has no eversion; keep the ankle icon
    ("inversion", "CALF_RAISE", None),
    ("seated marching", "WARM_UP", "ANKLE_CIRCLES"),
    # conditioning: the library would read "bike" as the outdoor-cycling sport
    ("bike", "CARDIO", None),
    ("rower", "CARDIO", None),
    ("cardio", "CARDIO", None),
)

# Words that describe the kit, not the movement: a candidate carrying one the
# athlete didn't ask for is only mildly wrong ("goblet squat" -> DUMBBELL_GOBLET_
# SQUAT is fine), so they cost less than a stray movement word.
EQUIPMENT_WORDS = frozenset(
    {"barbell", "dumbbell", "kettlebell", "cable", "machine", "smith", "band",
     "banded", "weighted", "plate", "bosu", "ring"}
)
# Categories that name a piece of kit rather than a movement pattern. Garmin files
# some ordinary moves under these (BACK_SQUAT lives only in SANDBAG), so they're a
# valid last resort — but a real movement category wins the tie.
KIT_CATEGORIES = frozenset(
    {"SUSPENSION", "SANDBAG", "BATTLE_ROPE", "SLED", "TIRE", "SLEDGE_HAMMER",
     "LADDER", "TOTAL_BODY", "CARDIO"}
)
WORD_ALIASES = {
    "db": "dumbbell", "bb": "barbell", "kb": "kettlebell", "sl": "single",
    "banded": "band", "resistance": "band",
}
FILLER_WORDS = frozenset({"the", "a", "an", "with", "and", "each", "per", "x"})

# The exercise is the head of the name; everything from here on is a coaching
# note ("(3s lower)", "— 60° isometric hold", ", low resistance"). Left in, it
# hijacks the match — the last word of "hip flexor stretch (kneeling)" is
# "kneeling", which is in no exercise Garmin has. The full name still reaches the
# watch as the step description.
QUALIFIER = re.compile(r"[(\[—–,;].*$")

# Below this, the nearest name is a guess rather than a match, and the wrong
# exercise on the watch is worse than a described one. Tuned against the playbook
# and the movements the coach actually prescribes (see tests/test_garmin_payload).
MIN_MATCH_SCORE = 0.55
# At or above this the name matches on every word that matters and we push it as
# is. Between the two the words line up but the movement may not, so the semantic
# fallback gets a look — see tools/exercise_match.py.
CONFIDENT_MATCH_SCORE = 0.9


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", QUALIFIER.sub("", name).lower()).strip()


def _stem(word: str) -> str:
    word = WORD_ALIASES.get(word, word)
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]  # CALF_RAISES and "calf raise" are the same movement
    return word


def _words(name: str, split_compounds: bool = False) -> list[str]:
    """The comparable words of a movement name.

    `split_compounds` breaks a word Garmin spells apart ("clamshell" ->
    CLAM_SHELLS) into its library halves. Only applied to the athlete's side —
    the library defines the vocabulary, so it is the one that gets to be right."""
    out: list[str] = []
    for raw in _normalize(name).split():
        word = _stem(raw)
        if word in FILLER_WORDS:
            continue
        halves = _decompound(word) if split_compounds else None
        out.extend(halves or [word])
    return out


def _decompound(word: str) -> list[str] | None:
    """["clam", "shell"] for "clamshell" — but only if Garmin knows both halves."""
    vocab = library_vocabulary()
    if word in vocab or len(word) < 6:
        return None
    for cut in range(3, len(word) - 2):
        head, tail = word[:cut], word[cut:]
        if head in vocab and tail in vocab:
            return [head, tail]
    return None


@lru_cache(maxsize=1)
def exercise_library() -> tuple[tuple[str, str, frozenset[str], str], ...]:
    """(category, exerciseName, words, squashed) for every exercise Garmin has."""
    raw = json.loads((DATA_DIR / "garmin_exercises.json").read_text(encoding="utf-8"))
    return tuple(
        (category, exercise, frozenset(_words(exercise)), "".join(_words(exercise)))
        for category, exercises in raw.items()
        for exercise in exercises
    )


@lru_cache(maxsize=1)
def library_vocabulary() -> frozenset[str]:
    """Every word Garmin uses in an exercise name."""
    return frozenset(word for _, _, words, _ in exercise_library() for word in words)


def _match_score(wanted: frozenset[str], candidate: frozenset[str]) -> float:
    """F1 over shared words, discounting equipment the athlete didn't ask for."""
    shared = wanted & candidate
    if not shared:
        return 0.0
    recall = len(shared) / len(wanted)
    cost = sum(0.5 if w in EQUIPMENT_WORDS else 1.0 for w in candidate - wanted)
    precision = len(shared) / (len(shared) + cost)
    return 2 * recall * precision / (recall + precision)


def best_garmin_match(name: str) -> tuple[tuple[str, str] | None, float]:
    """The closest (category, exerciseName) in Garmin's library, and how sure we are.

    The last word of the name is the movement ("single-leg BRIDGE"), and a match
    that misses it isn't the same exercise however many other words it shares —
    without that rule "single-leg bridge", "single-leg circles" and "single-leg
    reach" all matched SINGLE_LEG_DIP. Exact matches modulo spacing are exempt,
    since they're the same word ("clamshell" == CLAM_SHELLS)."""
    words = _words(name, split_compounds=True)
    if not words:
        return None, 0.0
    wanted, movement, squashed = frozenset(words), words[-1], "".join(words)

    best: tuple[str, str] | None = None
    best_rank: tuple = ()
    for category, exercise, candidate, candidate_squashed in exercise_library():
        if candidate_squashed == squashed:
            score = 1.0
        elif movement not in candidate:
            continue
        else:
            score = _match_score(wanted, candidate)
        if score < MIN_MATCH_SCORE:
            continue
        # ties: prefer a category that names the movement (PLANK/PLANK over
        # SUSPENSION/PLANK), then a movement category over a kit one, then the
        # least-embellished name — and stay deterministic after that.
        rank = (
            score,
            bool(frozenset(_words(category)) & candidate),
            category not in KIT_CATEGORIES,
            -len(candidate),
            -len(exercise),
        )
        if rank > best_rank:
            best, best_rank = (category, exercise), rank
    return best, (best_rank[0] if best_rank else 0.0)


def nearest_garmin_exercise(name: str) -> tuple[str, str] | None:
    return best_garmin_match(name)[0]


def classify_garmin_exercise(name: str) -> tuple[str | None, str | None]:
    """(category, exerciseName) for a movement — either may be None."""
    return _classify(name)[0]


def _classify(name: str) -> tuple[tuple[str | None, str | None], float]:
    normalized = _normalize(name)
    for needle, category, exercise in EXERCISE_OVERRIDES:
        if needle in normalized:
            return (category, exercise), 1.0  # hand-verified; nothing to second-guess
    matched, score = best_garmin_match(name)
    return (matched if matched else (None, None)), score


def classify_all(
    names: Iterable[str], resolver: "Resolver | None" = None
) -> dict[str, tuple[str | None, str | None]]:
    """Classify a whole session, handing anything doubtful to `resolver`.

    Doubtful means no match *or* a lukewarm one, because sharing words with an
    exercise is not the same as being it: "Tibialis raise" scores well against
    PLATE_RAISES and "Monster walk" against WALK, and both are the wrong movement
    on the watch. Only a confident match is trusted on its own.

    `resolver` is the injected side effect — without one this is pure and offline,
    which is how the tests and every payload-shaping caller run it."""
    scored = {name: _classify(name) for name in names}
    classified = {name: pair for name, (pair, _) in scored.items()}
    if not resolver:
        return classified

    doubtful = [
        name
        for name, ((category, _), score) in scored.items()
        if category is None or score < CONFIDENT_MATCH_SCORE
    ]
    if doubtful:
        # the model's answer is validated against the library, so it either
        # improves on the guess or leaves it alone
        classified.update(resolver(doubtful))
    return classified


SPORT_TYPES: dict[str, dict[str, Any]] = {
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "strength_training": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "mobility": {"sportTypeId": 11, "sportTypeKey": "mobility"},
    "conditioning": {"sportTypeId": 11, "sportTypeKey": "mobility"},
}


def _emit_step(
    order: int,
    *,
    name: str,
    sets: int,
    reps: int | None,
    time_sec: int | None,
    weight_kg: float | None,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build one executable step (wrapped in a RepeatGroupDTO when sets>1),
    encoding the hard-won Garmin quirks (see docs/garmin_strength.md).

    Condition type IDs: 2 = time, 7 = iterations, 10 = reps — numeric id is
    mandatory; the value goes in step-level endConditionValue."""
    if reps:
        end_condition = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
        end_value: float = reps
    else:
        end_condition = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        end_value = time_sec or 60
    entry: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "description": name,
    }
    order += 1
    if weight_kg is not None:
        entry["weightValue"] = weight_kg
        entry["weightUnit"] = {"unitKey": "kilogram"}
    category, exercise_name = (classified or {}).get(name) or classify_garmin_exercise(name)
    if category:
        entry["category"] = category
    if exercise_name:
        entry["exerciseName"] = exercise_name

    if sets > 1:
        group = {
            "type": "RepeatGroupDTO",
            "stepOrder": entry["stepOrder"],
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "numberOfIterations": sets,
            "smartRepeat": False,
            "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
            "endConditionValue": sets,
            "workoutSteps": [{**entry, "stepOrder": order}],
        }
        order += 1
        return [group], order
    return [entry], order


def _wrap_payload(name: str, sport_key: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    sport = SPORT_TYPES.get(sport_key, SPORT_TYPES["strength"])
    return {
        "workoutName": name,
        "sportType": sport,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
        ],
    }


def parse_exercise_sets(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a Garmin exerciseSets payload into flat set rows.

    Pure (fixture-testable). Only ACTIVE sets count; Garmin reports weight in
    GRAMS (18000.0 = 18 kg) and sometimes logs reps=0 when the watch missed
    the count — those rows are kept (a set happened) with reps=None."""
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(raw.get("exerciseSets") or []):
        if s.get("setType") != "ACTIVE":
            continue
        exercises = s.get("exercises") or [{}]
        ex = exercises[0]
        reps = s.get("repetitionCount")
        weight = s.get("weight")
        rows.append(
            {
                "set_index": i,
                "category": ex.get("category"),
                "exercise_name": ex.get("name"),
                "reps": int(reps) if reps else None,
                "weight_kg": round(weight / 1000, 2) if weight else None,
                "duration_sec": s.get("duration"),
            }
        )
    return rows


def get_exercise_sets(activity_id: str) -> list[dict[str, Any]]:
    """ACTIVE sets of a strength activity as normalized rows."""
    api = client()
    return parse_exercise_sets(api.get_activity_exercise_sets(activity_id) or {})


def build_strength_payload(
    session: StructuredSession, resolver: "Resolver | None" = None
) -> dict[str, Any]:
    """Garmin workout-API JSON for a composed session (verified schema)."""
    classified = classify_all([s.exercise for s in session.steps], resolver)
    steps: list[dict[str, Any]] = []
    order = 1
    for step in session.steps:
        emitted, order = _emit_step(
            order,
            name=step.exercise,
            sets=step.sets,
            reps=step.reps,
            time_sec=step.duration_sec,
            weight_kg=step.weight_kg,
            classified=classified,
        )
        steps.extend(emitted)
    return _wrap_payload(session.title, "strength", steps)


def build_template_payload(
    template: "WorkoutTemplate", resolver: "Resolver | None" = None
) -> dict[str, Any]:
    """Garmin workout-API JSON for a playbook template (warmup + all blocks).

    Block-level `sets` (strength supersets) wrap the whole block in a repeat;
    exercise-level `sets` wrap a single move. Used to materialize PT/base
    routines that don't yet exist as Garmin workouts (e.g. home PT)."""
    exercises = [*template.warmup, *(e for b in template.blocks for e in b.exercises)]
    classified = classify_all([e.name for e in exercises], resolver)
    steps: list[dict[str, Any]] = []
    order = 1
    for ex in template.warmup:
        emitted, order = _emit_step(
            order, name=ex.name, sets=ex.sets or 1, reps=ex.reps,
            time_sec=ex.time_sec, weight_kg=None, classified=classified,
        )
        steps.extend(emitted)
    for block in template.blocks:
        block_steps: list[dict[str, Any]] = []
        for ex in block.exercises:
            emitted, order = _emit_step(
                order, name=ex.name, sets=ex.sets or 1, reps=ex.reps,
                time_sec=ex.time_sec, weight_kg=None, classified=classified,
            )
            block_steps.extend(emitted)
        if block.sets and block.sets > 1:
            group = {
                "type": "RepeatGroupDTO",
                "stepOrder": block_steps[0]["stepOrder"],
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": block.sets,
                "smartRepeat": False,
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                "endConditionValue": block.sets,
                "workoutSteps": block_steps,
            }
            steps.append(group)
        else:
            steps.extend(block_steps)
    return _wrap_payload(template.label, template.sport, steps)


def create_garmin_workout(session: StructuredSession) -> WorkoutRef:
    """Create a structured workout via the workout API (JSON path, NOT FIT upload).

    This is the only path that reaches for the semantic fallback: a movement the
    string matcher can't place would otherwise land on the watch as a bare note,
    and here we're about to push it for real."""
    from jim.tools.exercise_match import semantic_resolver  # deferred: needs db + LLM

    api = client()
    payload = build_strength_payload(session, resolver=semantic_resolver())
    resp = api.upload_workout(payload)
    workout_id = str(resp.get("workoutId", ""))
    log.info("created garmin workout %s (%s)", workout_id, session.title)
    return WorkoutRef(workout_id=workout_id)


def schedule_workout(workout_id: str, on: date) -> None:
    api = client()
    api.schedule_workout(workout_id, on.isoformat())
    log.info("scheduled workout %s for %s", workout_id, on)


def clear_schedule(on: date) -> None:
    """Unschedule every planned (not completed) workout on `on`.

    Used by the morning re-plan before pushing a replacement, so a stale
    nightly schedule doesn't sit next to the new one. Only touches calendar
    items of type 'workout' — recorded activities are untouched."""
    api = client()
    calendar = api.get_scheduled_workouts(on.year, on.month) or {}
    for item in calendar.get("calendarItems", []):
        if item.get("itemType") == "workout" and item.get("date") == on.isoformat():
            api.unschedule_workout(item["id"])
            log.info("unscheduled stale workout %s (%s) on %s", item.get("id"),
                     item.get("title"), on)
