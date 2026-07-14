"""Re-vendor Garmin's exercise taxonomy into src/jim/data/garmin_exercises.json.

The taxonomy is a fixed enum on Garmin's side: a step's `category` must be one of
its categories, and `exerciseName` must be one of that category's exercises, or
the workout API rejects the payload ("Invalid category") or the step lands on the
watch as a bare description with no exercise attached.

Garmin publishes the whole list as static web data, so we vendor it rather than
hand-maintaining a lookup table. It changes rarely; run this if a movement that
plainly exists on Garmin isn't being matched.

    python scripts/refresh_garmin_exercises.py

Only the muscle metadata is dropped — we keep every (category, exercise) pair.
"""

import json
import pathlib
import urllib.request

SOURCE = "https://connect.garmin.com/web-data/exercises/Exercises.json"
TARGET = pathlib.Path(__file__).resolve().parent.parent / "src/jim/data/garmin_exercises.json"


def main() -> None:
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:  # noqa: S310 — fixed https URL
        categories = json.load(resp)["categories"]

    slim = {cat: sorted(body["exercises"]) for cat, body in sorted(categories.items())}
    if not slim or sum(len(v) for v in slim.values()) < 500:
        raise SystemExit(f"refusing to write a suspiciously small taxonomy: {slim.keys()}")

    TARGET.write_text(json.dumps(slim, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"wrote {TARGET.relative_to(pathlib.Path.cwd())}: "
        f"{len(slim)} categories, {sum(len(v) for v in slim.values())} exercises"
    )


if __name__ == "__main__":
    main()
