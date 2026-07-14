# Garmin strength workout JSON (M1)

**Status: VERIFIED server-side 2026-07-07** (account: personal, via
`python-garminconnect` 0.3.2 workout API). Workout `1624597366` created and
scheduled for 2026-07-08; steps, reps, weights, and repeat groups all persist
correctly on Garmin's side. Remaining check: visual confirmation on the watch
after sync.

## Rules learned the hard way (each was a live 400/dropped field)

1. **`conditionTypeId` is mandatory** — sending only `conditionTypeKey` fails
   with `Invalid WorkoutConditionTypeDTO id (provided value: 0)`.
   IDs: `2` = time (seconds), `7` = iterations, `10` = reps.
2. **The value goes in step-level `endConditionValue`** — a `conditionValue`
   nested inside `endCondition` is *silently dropped* (workout saves, reps
   vanish).
3. **Sets are a `RepeatGroupDTO`**, not a field: `numberOfIterations` on an
   `ExecutableStepDTO` is silently dropped. Wrap the exercise step in a repeat
   group with `stepType {6, repeat}`, `endCondition {7, iterations}`, and
   `numberOfIterations` = sets.
4. **`category` must be a Garmin taxonomy enum** (`SQUAT`, `DEADLIFT`,
   `CALF_RAISE`, `PLANK`, …) or omitted — free text fails with
   `Invalid category`. `exerciseName` (e.g. `GOBLET_SQUAT`, `SIDE_PLANK`) must
   be one of *that category's* exercises; an invented pair is rejected or
   silently dropped. See "Matching a movement" below.
5. **Weight**: `weightValue` (kg) + `weightUnit: {"unitKey": "kilogram"}`.
6. Write path is the **workout API** (`POST .../workout` via
   `upload_workout`), NOT FIT upload. Schedule with
   `schedule_workout(workout_id, "YYYY-MM-DD")` — it lands as a calendar item
   (`itemType: workout`).

## Verified payload shape

```json
{
  "workoutName": "…",
  "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
  "workoutSegments": [
    {
      "segmentOrder": 1,
      "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
      "workoutSteps": [
        {
          "type": "RepeatGroupDTO",
          "stepOrder": 1,
          "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
          "numberOfIterations": 3,
          "smartRepeat": false,
          "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
          "endConditionValue": 3,
          "workoutSteps": [
            {
              "type": "ExecutableStepDTO",
              "stepOrder": 2,
              "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
              "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
              "endConditionValue": 8,
              "description": "Goblet squat",
              "weightValue": 16,
              "weightUnit": {"unitKey": "kilogram"},
              "category": "SQUAT",
              "exerciseName": "GOBLET_SQUAT"
            }
          ]
        }
      ]
    }
  ]
}
```

Timed steps (planks, PT holds) use `endCondition {2, time}` with
`endConditionValue` in seconds.

## Matching a movement to Garmin's exercise library

A step with no `category` reaches the watch as a **bare description** — a note,
with no exercise attached, no animation, and no per-set logging. So every
movement is matched to the closest thing Garmin actually has.

Garmin's taxonomy is a closed enum, published as static web data. We vendor the
whole thing (47 categories, ~1500 exercises) at `src/jim/data/garmin_exercises.json`
rather than hand-maintaining a lookup table; `scripts/refresh_garmin_exercises.py`
re-pulls it. Matching then goes:

1. **`EXERCISE_OVERRIDES`** — the handful of movements where the nearest name is
   the *wrong* movement, or Garmin simply has no equivalent. Mostly knee/ankle
   PT, which the taxonomy barely covers: it has no ankle eversion at all, and its
   nearest name to a wall sit is `WEIGHTED_WALL_SQUAT`, a different exercise under
   load. Hand-verified; first match wins.
2. **Nearest name in the library**, scored on shared words (F1), discounting
   equipment words the athlete didn't ask for — a goblet squat is still a goblet
   squat if Garmin files it under `DUMBBELL_`.
3. **The semantic fallback** (`tools/exercise_match.py`) for anything the words
   can't settle — see below.
4. **Still nothing** → description only.

### The semantic fallback

Words fail in one way: when the athlete's vocabulary and Garmin's share none.
"Copenhagen plank" finds `PLANK` because they both say plank; `GHR`, `hip
airplane` and `Pallof press` find nothing. Worse, a lexical match can be
confidently *wrong* — "Tibialis raise" scores well against `PLATE_RAISES` and
"Monster walk" against `WALK`, and both are the wrong movement on the watch.

So anything below `CONFIDENT_MATCH_SCORE` — no match, or a lukewarm one — goes to
a model, which knows these are the same movements Garmin files under other names.
Four properties keep it honest and cheap:

- **Lexical first.** ~49 of the 57 playbook movements match confidently, so a
  normal push costs zero tokens.
- **One call per push**, not one per exercise: the session's doubtful names batch.
- **Cached in `kv` (`exercise_map`), negatives included** — a name is paid for
  once, ever. Delete the key to re-ask.
- **Validated against the library.** The model will invent enums:
  `CORE/SINGLE_LEG_CIRCLES` was exactly that mistake, and it doesn't exist. Any
  answer that isn't a real (category, exercise) pair is discarded and the lexical
  guess stands. A described step beats a wrong one.

It's a side effect, so it's injected: only `create_garmin_workout` reaches for it,
and the payload builders take a `resolver` that tests hand a fake. Without one
they're pure and offline.

Three rules earn their keep, each from a real mis-push:

- **The last word is the movement.** A candidate that misses it isn't the same
  exercise however many other words it shares — otherwise "single-leg bridge",
  "single-leg circles" and "single-leg reach" all match `SINGLE_LEG_DIP`.
- **Trailing qualifiers are notes, not the name.** `Leg extension — 60° isometric
  hold` ends in "hold", which is in no exercise Garmin has; matching uses the head
  (`Leg extension`) and the full text still goes in `description`.
- **Compounds are split against Garmin's own vocabulary.** `clamshell` →
  `CLAM_SHELLS`.

Ties prefer a category that names the movement (`PLANK/PLANK` over
`SUSPENSION/PLANK`) and a movement category over a kit one — though a kit
category is a valid last resort, since Garmin files some ordinary moves only
there (`BACK_SQUAT` lives under `SANDBAG`).

`tests/test_garmin_payload.py` asserts every playbook movement matches, and that
every override is a pair Garmin will actually accept.

## Auth notes for this deployment

Login from datacenter IPs gets Cloudflare-blocked/rate-limited (429/403), and
the `curl_cffi` transport is incompatible with proxied environments. Fix:
mint tokens once from a residential IP (`Garmin(email, pw).login(path)`), copy
`garmin_tokens.json` to `~/.garminconnect` on the server. Token logins + API
calls work fine from cloud IPs, and the DI refresh token renews the session.
