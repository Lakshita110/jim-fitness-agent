"""M1 — write round-trip (riskiest first, PLAN.md §9).

Creates + schedules ONE hardcoded strength session via the workout API and
prints the accepted payload. Run locally with real Garmin creds in .env:

    python scripts/m1_roundtrip.py [YYYY-MM-DD]

Done when the workout appears on the watch after sync; document the exact
accepted JSON shape in docs/garmin_strength.md."""

import json
import sys
from datetime import date, timedelta

from jim.auth import first_user_id
from jim.schemas import ExerciseStep, StructuredSession
from jim.tools.garmin import build_strength_payload, create_garmin_workout, schedule_workout

HARDCODED = StructuredSession(
    for_date=date.today() + timedelta(days=1),
    kind="strength",
    title="Jim M1 round-trip test",
    steps=[
        ExerciseStep(exercise="Goblet squat", sets=3, reps=8, weight_kg=16),
        ExerciseStep(exercise="Romanian deadlift", sets=3, reps=8, weight_kg=40),
        ExerciseStep(exercise="Single-leg calf raise", sets=3, reps=12),
        ExerciseStep(exercise="Side plank", sets=2, duration_sec=40),
    ],
    est_duration_min=35,
)


def main() -> None:
    user_id = first_user_id()
    if user_id is None:
        raise SystemExit("no users in the database — run scripts/backfill_users.py first")
    on = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else HARDCODED.for_date
    print("payload we are sending:")
    print(json.dumps(build_strength_payload(HARDCODED), indent=2))
    ref = create_garmin_workout(user_id, HARDCODED)
    print(f"created workout {ref.workout_id}; scheduling for {on}")
    schedule_workout(user_id, ref.workout_id, on)
    print("done — sync the watch and confirm, then update docs/garmin_strength.md")


if __name__ == "__main__":
    main()
