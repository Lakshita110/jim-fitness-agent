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
   `Invalid category`. `exerciseName` (e.g. `GOBLET_SQUAT`, `SIDE_PLANK`)
   is optional and refines the on-watch display/animation. The mapping lives
   in `EXERCISE_TAXONOMY` in `tools/garmin.py`; unmapped movements omit
   category and rely on `description`.
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

## Auth notes for this deployment

Login from datacenter IPs gets Cloudflare-blocked/rate-limited (429/403), and
the `curl_cffi` transport is incompatible with proxied environments. Fix:
mint tokens once from a residential IP (`Garmin(email, pw).login(path)`), copy
`garmin_tokens.json` to `~/.garminconnect` on the server. Token logins + API
calls work fine from cloud IPs, and the DI refresh token renews the session.
