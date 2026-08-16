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
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jim.schemas import ActivitySummary, GarminToday, StructuredSession, WorkoutRef

if TYPE_CHECKING:
    from jim.tools.exercise_match import Resolver

log = logging.getLogger(__name__)

_clients: dict[int, Any] = {}


TOKEN_STORE = "~/.garminconnect"
# garminconnect's login() switches on length: >512 chars = token data, else a
# filesystem path. Anything shorter than this is a mangled blob, not a session.
MIN_TOKEN_BLOB_CHARS = 512

# Prefix every one-off workout mcp_server.create_or_update_workout creates,
# so jobs/nightly.py can tell "a Claude adaptation for one day" apart from a
# real named workout in the athlete's library (Full Body A, PT Day, ...)
# purely by reading Garmin's own data — no separate Jim-side tracking table,
# since Garmin is the source of truth for what it's holding.
ADAPTED_WORKOUT_PREFIX = "Jim · "


def client(user_id: int) -> Any:
    """Lazily authenticated Garmin client for `user_id` (cached per process,
    re-login on expiry via re-authentication).

    Two token sources, in order, read from this user's `user_credentials` row:
    1. garmin_tokens — a session blob (scripts/garmin_login.py --export, or the
       Settings -> Garmin connect flow). This is what deployed containers use:
       their filesystem is ephemeral and a fresh SSO login would block on an
       MFA prompt with no stdin to answer it. `login()` treats a string >512
       chars as token data rather than a path.
    2. garmin_password — fallback re-auth when there's no usable token blob.
    """
    if user_id not in _clients:
        from garminconnect import (
            Garmin,
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        )

        from jim.db import get_user_credentials

        creds = get_user_credentials(user_id)
        if not creds or not (creds.get("garmin_tokens") or creds.get("garmin_password")):
            raise RuntimeError(f"user {user_id} has not connected Garmin")
        garmin = Garmin(creds.get("garmin_email") or "", creds.get("garmin_password") or "")
        tokens = (creds.get("garmin_tokens") or "").strip()
        if tokens:
            # login() only treats the string as token data above 512 chars —
            # below that it silently falls back to reading it as a PATH, which
            # fails in a confusing way. Catch a truncated/mangled blob here.
            if len(tokens) <= MIN_TOKEN_BLOB_CHARS:
                raise RuntimeError(
                    f"user {user_id}'s garmin_tokens is only {len(tokens)} chars; a real"
                    f" session blob is >{MIN_TOKEN_BLOB_CHARS}. It was likely truncated."
                )
            log.info("garmin: authenticating user %s from stored tokens blob", user_id)
            login_args: tuple[str, ...] = (tokens,)
        else:
            log.info("garmin: authenticating user %s from stored password", user_id)
            login_args = ()
        try:
            garmin.login(*login_args)
        except GarminConnectAuthenticationError as e:
            raise RuntimeError(
                f"Garmin login failed for user {user_id} — the stored session/password"
                " is no longer valid. Reconnect Garmin in Settings."
            ) from e
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as e:
            raise RuntimeError(
                "Garmin is temporarily unreachable — try again shortly."
            ) from e
        except Exception as e:
            # garminconnect talks to an undocumented API, so failures beyond the
            # three typed exceptions above (network blips, unexpected response
            # shapes, etc.) are common — surface a clean message instead of a
            # raw exception bubbling out of every read/write call.
            log.exception("unexpected error during garmin login for user %s", user_id)
            raise RuntimeError(
                f"Garmin login failed unexpectedly for user {user_id}."
            ) from e
        _clients[user_id] = garmin
    return _clients[user_id]


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


def get_garmin_today(user_id: int, day: date) -> GarminToday:
    """Activities + recovery for `day`. Computation done here; returns summary."""
    api = client(user_id)
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


def get_training_readiness(user_id: int, day: date) -> dict:
    """Garmin's own dedicated readiness verdict + the specific factors behind
    it (sleep, HRV, recovery time, acute load, stress) — richer than the
    single trainingReadinessScore number get_garmin_today pulls out of the
    generic daily stats blob."""
    api = client(user_id)
    result = api.get_training_readiness(day.isoformat())
    # Garmin returns a list with one entry per day queried; cdate is a single day.
    if isinstance(result, list):
        return result[0] if result else {}
    return result or {}


def get_training_status(user_id: int, day: date) -> dict:
    """Garmin's own training-load verdict (productive, peaking, overreaching,
    detraining, unproductive, ...) plus VO2max trend — a second, differently-
    computed opinion alongside Jim's own ACWR-based readiness_read."""
    api = client(user_id)
    return api.get_training_status(day.isoformat()) or {}


def get_daily_steps(user_id: int, start: date, end: date) -> list[dict]:
    """Daily step counts (and Garmin's own step goal) for [start, end]."""
    api = client(user_id)
    return api.get_daily_steps(start.isoformat(), end.isoformat()) or []


def backfill_if_empty(user_id: int, today: date, days: int = 90) -> None:
    """First-ever real Garmin data for this user: garmin_daily has zero
    rows, so query_history/readiness_read would have nothing to work with
    even after today's own sync. Pull the trailing `days` of history once,
    the same way scripts/backfill.py does by hand — this just makes that
    step automatic instead of something the operator has to remember to run
    per new signup.

    Called from two places, not the nightly cron fan-out: web/garmin_routes.py
    right after a Garmin connect/MFA succeeds (a single user, on a request
    the athlete is already waiting on), and mcp_server.py's read tools as a
    second safety net for any account that got its Garmin credentials
    another way. ~90 sequential Garmin calls per new signup would blow the
    nightly cron's shared 60s Vercel budget for every other user in the same
    run, which is why this never runs from there.

    Gated on already having *any* history, which misses the case of an
    account that connected Garmin before this existed and only has a few
    days synced — use backfill_history directly (mcp_server.backfill_history
    tool) to force a re-pull regardless of what's already there."""
    from jim.db import connect

    with connect() as conn:
        already_has_history = conn.execute(
            "SELECT 1 FROM garmin_daily WHERE user_id = %s LIMIT 1", (user_id,)
        ).fetchone()
    if already_has_history:
        return
    backfill_history(user_id, today, days)


def backfill_history(user_id: int, today: date, days: int = 90) -> None:
    """Unconditionally pull the trailing `days` of Garmin history into
    Postgres (upserts throughout, so safe to re-run / overlap). This is the
    actual work backfill_if_empty gates; call it directly to force a re-pull
    for an account that already has some history but not the full window
    (e.g. connected before backfill_if_empty existed)."""
    from jim.db import connect
    from jim.jobs.nightly import STRENGTH_TYPES, store_exercise_sets

    log.info("backfilling %d days for user %s", days, user_id)
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        snapshot = get_garmin_today(user_id, day)
        with connect() as conn:
            conn.execute(
                "INSERT INTO garmin_daily (user_id, day, hrv, sleep_hours, body_battery,"
                " readiness, resting_hr, raw) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (user_id, day) DO UPDATE SET hrv=EXCLUDED.hrv,"
                " sleep_hours=EXCLUDED.sleep_hours, body_battery=EXCLUDED.body_battery,"
                " readiness=EXCLUDED.readiness, resting_hr=EXCLUDED.resting_hr,"
                " raw=EXCLUDED.raw",
                (user_id, day, snapshot.hrv, snapshot.sleep_hours, snapshot.body_battery,
                 snapshot.readiness, snapshot.resting_hr, snapshot.model_dump_json()),
            )
            for act in snapshot.activities:
                conn.execute(
                    "INSERT INTO garmin_activities (user_id, activity_id, day, type,"
                    " duration_min, training_load, summary)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (user_id, activity_id) DO NOTHING",
                    (user_id, act.activity_id, day, act.type, act.duration_min,
                     act.training_load, act.model_dump_json()),
                )
                if act.type in STRENGTH_TYPES:
                    try:
                        store_exercise_sets(
                            conn, user_id, act.activity_id, day,
                            get_exercise_sets(user_id, act.activity_id),
                        )
                    except Exception:
                        log.exception("sets fetch failed for %s", act.activity_id)
            conn.commit()
    log.info("backfill done for user %s", user_id)


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


# Every entry below is live-verified against this athlete's real Garmin
# account (create -> read back the stored sportType, not guessed from any
# doc — Garmin publishes no official API docs and the reverse-engineered
# community lists disagree with each other and with this). "hiking" has no
# dedicated Garmin sportType at all; "other" is the closest real fit.
SPORT_TYPES: dict[str, dict[str, Any]] = {
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "strength_training": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
    "mobility": {"sportTypeId": 11, "sportTypeKey": "mobility"},
    "yoga": {"sportTypeId": 7, "sportTypeKey": "yoga"},
    "pilates": {"sportTypeId": 8, "sportTypeKey": "pilates"},
    "conditioning": {"sportTypeId": 6, "sportTypeKey": "cardio_training"},
    "running": {"sportTypeId": 1, "sportTypeKey": "running"},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    "swimming": {"sportTypeId": 4, "sportTypeKey": "swimming"},
    "walking": {"sportTypeId": 12, "sportTypeKey": "walking"},
    "hiking": {"sportTypeId": 3, "sportTypeKey": "other"},
    "hiit": {"sportTypeId": 9, "sportTypeKey": "hiit"},
    "rucking": {"sportTypeId": 13, "sportTypeKey": "rucking"},
    "other": {"sportTypeId": 3, "sportTypeKey": "other"},
}

# Kinds whose steps are real Garmin exercise-library movements (push-ups,
# squats, stretches, ...) — everything else (a run, a ride, a walk) is an
# activity, not an "exercise", and forcing it through the strength taxonomy
# produces a wrong or empty category/exerciseName match.
_TAXONOMY_CLASSIFIED_KINDS = {"strength", "mobility"}


# Garmin's own step role (stepType). "interval" is the default every step
# used before role support existed. Live-verified against a real account —
# including 7/"other" and 8/"main", which appear in no documentation
# (official or reverse-engineered) found anywhere; only found by directly
# probing ids beyond the commonly-cited 1-6. "rest" (5) and "repeat" (6)
# aren't here: rest is its own self_paced_rest flag (a rest step also needs
# a different endCondition, not just a different stepType) and repeat is
# the structural RepeatGroupDTO wrapping mechanism, not a per-step choice.
_STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "other": {"stepTypeId": 7, "stepTypeKey": "other"},
    "main": {"stepTypeId": 8, "stepTypeKey": "main"},
}


def _build_entry(
    order: int,
    *,
    name: str,
    reps: int | None,
    time_sec: int | None,
    weight_kg: float | None,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
    self_paced_rest: bool = False,
    role: str = "interval",
    distance_m: float | None = None,
    target_heart_rate_zone: int | None = None,
    target_power_zone: int | None = None,
    target_pace_min_mps: float | None = None,
    target_pace_max_mps: float | None = None,
    secondary_target_cadence_min: float | None = None,
    secondary_target_cadence_max: float | None = None,
    end_at_heart_rate_bpm: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Build one bare step (not wrapped in any repeat block) — the shared
    building block both a single exercise's own repeat and a multi-exercise
    superset's shared repeat are made of. See _emit_step and _emit_superset
    for the two ways this gets wrapped.

    End condition priority when more than one is set: reps, then distance_m,
    then end_at_heart_rate_bpm, then time_sec (falling back to a 60s default
    if none are set at all). Condition type IDs, all live-verified: 2 =
    time, 3 = distance, 6 = heart.rate, 7 = iterations, 10 = reps — numeric
    id is mandatory; the value goes in step-level endConditionValue (meters
    for distance, bpm for heart rate). Whether Garmin treats the heart-rate
    condition as "until at/below" or "until at/above" the value wasn't
    independently confirmed.

    `self_paced_rest=True` builds Garmin's actual press-to-continue rest
    step (stepTypeId 5 "rest", endCondition conditionTypeId 1 "lap.button")
    instead of a timed interval standing in for one — the athlete taps the
    watch to advance rather than waiting out a fixed timer. Live-verified
    against a real account; not documented anywhere. When set, this
    overrides `role`, every end-condition field, weight, target zones, and
    exercise classification — none of those apply to a rest step.

    `role` picks the stepType (see _STEP_TYPES) — a real warmup/cooldown/
    recovery block shows correctly on the watch instead of every step being
    a generic interval.

    `target_heart_rate_zone`/`target_power_zone` (Garmin's own zoneNumber,
    typically 1-5) and `target_pace_min_mps`/`target_pace_max_mps` (a speed
    range) all live-verified as accepted; heart rate wins if both zone
    fields are set. Pace's exact unit convention (assumed m/s) was not
    independently confirmed against what displays on the watch — see
    ExerciseStep's docstring.

    `secondary_target_cadence_min`/`_max` add a SECOND target alongside the
    primary one — e.g. heart-rate zone as primary plus a cadence range on
    top. Only meaningful when a primary target (heart rate or power zone)
    is also set; live-verified accepted either way, but has no effect
    without a primary target since Garmin still needs to know what
    "secondary" is relative to.

    `classify=False` skips matching `name` against Garmin's strength exercise
    taxonomy (category/exerciseName) — that taxonomy is push-ups/squats/etc,
    and forcing something like "Walk" through it produces a wrong or empty
    match that can get the whole step rejected. Conditioning sessions (walks,
    runs, rides — activities, not exercises) pass classify=False and rely on
    the plain `description` instead; strength/mobility sessions still get
    classified since their steps really are Garmin exercise-library moves."""
    if self_paced_rest:
        entry = {
            "type": "ExecutableStepDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": 5, "stepTypeKey": "rest"},
            "endCondition": {"conditionTypeId": 1, "conditionTypeKey": "lap.button"},
            "endConditionValue": None,
            "description": name,
        }
        return entry, order + 1

    if reps:
        end_condition = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
        end_value: float = reps
    elif distance_m is not None:
        end_condition = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
        end_value = distance_m
    elif end_at_heart_rate_bpm is not None:
        end_condition = {"conditionTypeId": 6, "conditionTypeKey": "heart.rate"}
        end_value = end_at_heart_rate_bpm
    else:
        end_condition = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        end_value = time_sec or 60
    entry = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_TYPES.get(role, _STEP_TYPES["interval"]),
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "description": name,
    }
    if weight_kg is not None:
        entry["weightValue"] = weight_kg
        entry["weightUnit"] = {"unitKey": "kilogram"}
    if target_heart_rate_zone is not None:
        entry["targetType"] = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
        entry["zoneNumber"] = target_heart_rate_zone
    elif target_power_zone is not None:
        entry["targetType"] = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone"}
        entry["zoneNumber"] = target_power_zone
    elif target_pace_min_mps is not None or target_pace_max_mps is not None:
        entry["targetType"] = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}
        entry["targetValueOne"] = target_pace_min_mps
        entry["targetValueTwo"] = target_pace_max_mps
    if secondary_target_cadence_min is not None or secondary_target_cadence_max is not None:
        entry["secondaryTargetType"] = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence"}
        entry["secondaryTargetValueOne"] = secondary_target_cadence_min
        entry["secondaryTargetValueTwo"] = secondary_target_cadence_max
    if classify:
        category, exercise_name = (classified or {}).get(name) or classify_garmin_exercise(name)
        if category:
            entry["category"] = category
        if exercise_name:
            entry["exerciseName"] = exercise_name
    return entry, order + 1


def _entry_from_step(
    order: int,
    step: Any,
    *,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
) -> tuple[dict[str, Any], int]:
    """_build_entry, but reading every field off an ExerciseStep-like object
    instead of a long parameter list — the shared adapter _emit_step and
    _emit_superset both use."""
    return _build_entry(
        order,
        name=step.exercise,
        reps=step.reps,
        time_sec=step.duration_sec,
        weight_kg=step.weight_kg,
        classified=classified,
        classify=classify,
        self_paced_rest=step.self_paced_rest,
        role=step.role,
        distance_m=step.distance_m,
        target_heart_rate_zone=step.target_heart_rate_zone,
        target_power_zone=step.target_power_zone,
        target_pace_min_mps=step.target_pace_min_mps,
        target_pace_max_mps=step.target_pace_max_mps,
        secondary_target_cadence_min=step.secondary_target_cadence_min,
        secondary_target_cadence_max=step.secondary_target_cadence_max,
        end_at_heart_rate_bpm=step.end_at_heart_rate_bpm,
    )


def _emit_step(
    order: int,
    step: Any,
    *,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """One exercise, wrapped in its own RepeatGroupDTO when sets>1 —
    Garmin's format for "Wall sit x5" as a single block, one exercise. For a
    superset (two-plus exercises sharing one round count), see
    _emit_superset instead; a repeat block wrapping only one exercise can't
    express that."""
    entry, order = _entry_from_step(order, step, classified=classified, classify=classify)
    if step.sets > 1:
        group = {
            "type": "RepeatGroupDTO",
            "stepOrder": entry["stepOrder"],
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "numberOfIterations": step.sets,
            "smartRepeat": False,
            "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
            "endConditionValue": step.sets,
            "workoutSteps": [{**entry, "stepOrder": order}],
        }
        order += 1
        return [group], order
    return [entry], order


def _emit_superset(
    order: int,
    steps: list[Any],
    *,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Two-plus exercises sharing ONE round count, wrapped in a single
    RepeatGroupDTO — "Round 1/3: Wall sit -> Row" as one grouped unit on the
    watch. This is Garmin's only way to express a superset: the repeat block
    itself doesn't care how many ExecutableStepDTOs it wraps, only
    _emit_step's single-exercise usage ever wrapped just one.

    `steps` must be ExerciseStep-like (has .exercise/.reps/.duration_sec/
    .weight_kg/.sets) and share superset_group already — the caller
    (build_strength_payload) is responsible for clustering. All steps must
    agree on `sets` (the shared round count); the first step's value wins if
    they don't, since Garmin has exactly one iteration count per block."""
    rounds = steps[0].sets
    group_order = order
    order += 1  # the group itself takes this slot; each exercise gets the next ones
    entries: list[dict[str, Any]] = []
    for step in steps:
        entry, order = _entry_from_step(order, step, classified=classified, classify=classify)
        entries.append(entry)
    group = {
        "type": "RepeatGroupDTO",
        "stepOrder": group_order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "numberOfIterations": rounds,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": rounds,
        "workoutSteps": entries,
    }
    return [group], order


def _wrap_payload(
    name: str, sport_key: str, steps: list[dict[str, Any]], notes: str = "",
) -> dict[str, Any]:
    sport = SPORT_TYPES.get(sport_key, SPORT_TYPES["strength"])
    payload: dict[str, Any] = {
        "workoutName": name,
        "sportType": sport,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
        ],
    }
    if notes:
        payload["description"] = notes
    return payload


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


def get_exercise_sets(user_id: int, activity_id: str) -> list[dict[str, Any]]:
    """ACTIVE sets of a strength activity as normalized rows."""
    api = client(user_id)
    return parse_exercise_sets(api.get_activity_exercise_sets(activity_id) or {})


def build_strength_payload(
    session: StructuredSession, resolver: "Resolver | None" = None
) -> dict[str, Any]:
    """Garmin workout-API JSON for a composed session (verified schema).

    `session.kind` drives both the sportType tag and whether steps get
    matched against Garmin's strength exercise taxonomy — a conditioning
    session's steps (walk, run, ride) aren't "exercises" in that taxonomy at
    all, so they skip classification and go out as plain description-only
    steps (see _emit_step's classify flag)."""
    classify = session.kind in _TAXONOMY_CLASSIFIED_KINDS
    classified = classify_all([s.exercise for s in session.steps], resolver) if classify else {}
    steps: list[dict[str, Any]] = []
    order = 1
    for is_pyramid, segment in _split_pyramid_segments(session.steps):
        if is_pyramid:
            emitted, order = _emit_pyramid(
                order, segment, classified=classified, classify=classify,
            )
            steps.extend(emitted)
        else:
            for cluster in _cluster_by(segment, "superset_group"):
                emitted, order = _emit_inner(
                    order, cluster, classified=classified, classify=classify,
                )
                steps.extend(emitted)
    return _wrap_payload(session.title, session.kind, steps, notes=session.rationale_summary)


def _split_pyramid_segments(steps: list[Any]) -> list[tuple[bool, list[Any]]]:
    """Split into (is_pyramid, steps) segments: consecutive steps sharing
    one non-null pyramid_group form a pyramid segment; any run of
    consecutive non-pyramid steps forms one plain segment, still intact for
    _cluster_by(..., "superset_group") to cluster normally afterward — a
    single `_cluster_by(steps, "pyramid_group")` pass alone would wrongly
    fragment non-pyramid steps into one-step segments before superset
    clustering ever saw them."""
    segments: list[tuple[bool, list[Any]]] = []
    for step in steps:
        is_pyramid = step.pyramid_group is not None
        if (
            segments
            and segments[-1][0] == is_pyramid
            and (not is_pyramid or segments[-1][1][-1].pyramid_group == step.pyramid_group)
        ):
            segments[-1][1].append(step)
        else:
            segments.append((is_pyramid, [step]))
    return segments


def _emit_inner(
    order: int,
    cluster: list[Any],
    *,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """One superset_group cluster (>1 step) or a single ungrouped step,
    emitted the normal way — the shared inner step ordinarily produces,
    whether or not it's also wrapped by an outer pyramid_group."""
    if len(cluster) > 1:
        return _emit_superset(order, cluster, classified=classified, classify=classify)
    (step,) = cluster
    return _emit_step(order, step, classified=classified, classify=classify)


def _emit_pyramid(
    order: int,
    steps: list[Any],
    *,
    classified: Mapping[str, tuple[str | None, str | None]] | None = None,
    classify: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """A repeat-of-repeats: wrap whatever `steps` would normally build
    (their own superset_group clusters, or plain single-exercise steps) in
    ONE outer RepeatGroupDTO — live-verified a RepeatGroupDTO can contain
    another RepeatGroupDTO. `steps` must already share pyramid_group (the
    caller is responsible for clustering); `pyramid_rounds` from the first
    step is the outer round count."""
    rounds = steps[0].pyramid_rounds
    group_order = order
    order += 1  # the outer group itself takes this slot
    inner: list[dict[str, Any]] = []
    for cluster in _cluster_by(steps, "superset_group"):
        emitted, order = _emit_inner(order, cluster, classified=classified, classify=classify)
        inner.extend(emitted)
    group = {
        "type": "RepeatGroupDTO",
        "stepOrder": group_order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "numberOfIterations": rounds,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": rounds,
        "workoutSteps": inner,
    }
    return [group], order


def _cluster_by(steps: list[Any], field: str) -> list[list[Any]]:
    """Group consecutive steps that share a non-null value for `field`
    (superset_group or pyramid_group) into one cluster each; every other
    step is its own single-item cluster. Only consecutive steps are merged
    — a group id repeated non-consecutively (e.g. group 1, group 2, group 1)
    is treated as two separate groups rather than silently reordered to
    merge them, since that would move exercises the athlete/Claude put in a
    specific order."""
    clusters: list[list[Any]] = []
    for step in steps:
        value = getattr(step, field)
        if (
            value is not None
            and clusters
            and clusters[-1]
            and getattr(clusters[-1][-1], field) == value
        ):
            clusters[-1].append(step)
        else:
            clusters.append([step])
    return clusters


def list_garmin_workouts(user_id: int) -> list[dict[str, str]]:
    """The athlete's existing Garmin workout library (named workouts they or
    Jim have saved). `get_workouts` paginates; 200 covers any real athlete's
    library in one round trip."""
    api = client(user_id)
    raw = api.get_workouts(start=0, limit=200) or []
    return [
        {
            "workout_id": str(w.get("workoutId", "")),
            "name": w.get("workoutName") or "",
            "sport": (w.get("sportType") or {}).get("sportTypeKey", ""),
        }
        for w in raw
    ]


def get_garmin_workout_detail(user_id: int, workout_id: str) -> dict[str, Any]:
    """Full step-by-step JSON for one workout, as `list_garmin_workouts` only
    returns name/id/sport."""
    api = client(user_id)
    return api.get_workout_by_id(workout_id)


def create_garmin_workout(user_id: int, session: StructuredSession) -> WorkoutRef:
    """Create a structured workout via the workout API (JSON path, NOT FIT upload).

    This is the only path that reaches for the semantic fallback: a movement the
    string matcher can't place would otherwise land on the watch as a bare note,
    and here we're about to push it for real."""
    from jim.tools.exercise_match import semantic_resolver  # deferred: needs db + LLM

    api = client(user_id)
    payload = build_strength_payload(session, resolver=semantic_resolver(user_id))
    resp = api.upload_workout(payload)
    workout_id = str(resp.get("workoutId", ""))
    log.info("created garmin workout %s (%s)", workout_id, session.title)
    return WorkoutRef(workout_id=workout_id)


def schedule_workout(user_id: int, workout_id: str, on: date) -> None:
    api = client(user_id)
    api.schedule_workout(workout_id, on.isoformat())
    log.info("scheduled workout %s for %s", workout_id, on)


def delete_garmin_workout(user_id: int, workout_id: str) -> None:
    """Remove a one-off adaptation from the athlete's workout library once
    it's no longer needed (superseded by a repush, or its scheduled day has
    passed). See jobs/nightly.cleanup_adapted_workouts, and
    mcp_server.cleanup_old_adapted_workouts for the on-demand equivalent."""
    api = client(user_id)
    api.delete_workout(workout_id)
    log.info("deleted garmin workout %s", workout_id)


def get_scheduled_workouts(user_id: int, start: date, end: date) -> list[dict]:
    """Workouts already on the real Garmin calendar in [start, end], for the
    draft-sync (a user who scheduled directly in Garmin Connect, or before ever
    using Jim, shouldn't see an empty plan just because Jim didn't propose it).

    Each item: {"date": date, "workout_id": str, "title": str}. `get_scheduled_
    workouts` is a per-month API, so a range spanning a month boundary means one
    call per distinct (year, month) touched, merged and filtered back to the
    exact range."""
    api = client(user_id)
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    out: list[dict] = []
    for year, month in months:
        calendar = api.get_scheduled_workouts(year, month) or {}
        for item in calendar.get("calendarItems", []):
            if item.get("itemType") != "workout":
                continue
            iso = item.get("date")
            if not iso:
                continue
            day = date.fromisoformat(iso)
            if not (start <= day <= end):
                continue
            workout_id = item.get("workoutId")
            if workout_id is None:
                continue  # not every calendar item carries one; skip rather than guess
            out.append({"date": day, "workout_id": str(workout_id), "title": item.get("title", "")})
    return out


def clear_schedule(user_id: int, on: date) -> None:
    """Unschedule every planned (not completed) workout on `on`.

    Used by the morning re-plan before pushing a replacement, so a stale
    nightly schedule doesn't sit next to the new one. Only touches calendar
    items of type 'workout' — recorded activities are untouched."""
    api = client(user_id)
    calendar = api.get_scheduled_workouts(on.year, on.month) or {}
    for item in calendar.get("calendarItems", []):
        if item.get("itemType") == "workout" and item.get("date") == on.isoformat():
            api.unschedule_workout(item["id"])
            log.info("unscheduled stale workout %s (%s) on %s", item.get("id"),
                     item.get("title"), on)
