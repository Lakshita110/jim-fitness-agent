"""Inspect and fix the exercise-match cache (kv key "exercise_map").

Every movement name the lexical matcher couldn't confidently place gets sent to
an LLM once, and the answer is cached here forever (tools/exercise_match.py) — so
a bad match sticks around just as permanently as a good one. This is how you look
at what's cached and correct it by hand without waiting for the entry to expire
(it never does) or truncating the whole cache.

    python scripts/exercise_map.py list                     # everything cached
    python scripts/exercise_map.py list "leg press"          # entries matching a substring
    python scripts/exercise_map.py show "hip airplane"       # one entry, exact (normalized)
    python scripts/exercise_map.py set "hip airplane" HIP_STABILITY HIP_CIRCLES
    python scripts/exercise_map.py set "some move" --none    # force "Garmin has nothing"
    python scripts/exercise_map.py forget "hip airplane"     # re-ask next time it's pushed
    python scripts/exercise_map.py clear                     # wipe the whole cache
"""

import sys

from jim.auth import first_user_id
from jim.db import kv_get, kv_set
from jim.tools.exercise_match import CACHE_KEY, valid_pairs
from jim.tools.garmin import _normalize


def cmd_list(user_id: int, substring: str = "") -> None:
    cache: dict = kv_get(user_id, CACHE_KEY) or {}
    needle = _normalize(substring)
    rows = sorted(k for k in cache if needle in k)
    if not rows:
        print("(nothing cached)" if not cache else f"(no entries matching {substring!r})")
        return
    for key in rows:
        value = cache[key]
        print(f"{key:42s} -> {tuple(value) if value else None}")
    print(f"\n{len(rows)} of {len(cache)} entries")


def cmd_show(user_id: int, name: str) -> None:
    cache: dict = kv_get(user_id, CACHE_KEY) or {}
    key = _normalize(name)
    if key not in cache:
        print(f"{name!r} is not cached — it will hit the LLM next time it's pushed")
        return
    print(f"{key} -> {tuple(cache[key]) if cache[key] else None}")


def cmd_set(user_id: int, name: str, category: str, exercise: str | None) -> None:
    allowed = valid_pairs()
    if category not in allowed:
        raise SystemExit(f"{category!r} is not a Garmin category. Known: {sorted(allowed)}")
    if exercise is not None and exercise not in allowed[category]:
        raise SystemExit(
            f"{exercise!r} is not in {category}. Some options: "
            f"{sorted(allowed[category])[:10]}"
        )
    cache: dict = kv_get(user_id, CACHE_KEY) or {}
    key = _normalize(name)
    cache[key] = [category, exercise] if exercise else [category, None]
    kv_set(user_id, CACHE_KEY, cache)
    print(f"{key} -> ({category}, {exercise})")


def cmd_set_none(user_id: int, name: str) -> None:
    cache: dict = kv_get(user_id, CACHE_KEY) or {}
    key = _normalize(name)
    cache[key] = None
    kv_set(user_id, CACHE_KEY, cache)
    print(f"{key} -> None (will describe-only, never re-ask the LLM)")


def cmd_forget(user_id: int, name: str) -> None:
    cache: dict = kv_get(user_id, CACHE_KEY) or {}
    key = _normalize(name)
    if cache.pop(key, "missing") == "missing":
        print(f"{name!r} was not cached")
        return
    kv_set(user_id, CACHE_KEY, cache)
    print(f"forgot {key!r} — it will hit the LLM next time it's pushed")


def cmd_clear(user_id: int) -> None:
    kv_set(user_id, CACHE_KEY, {})
    print("cleared the whole exercise_map cache")


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    user_id = first_user_id()
    if user_id is None:
        raise SystemExit("no users in the database — run scripts/backfill_users.py first")
    command, *rest = argv
    if command == "list":
        cmd_list(user_id, *rest)
    elif command == "show":
        cmd_show(user_id, *rest)
    elif command == "set":
        if len(rest) == 2 and rest[1] == "--none":
            cmd_set_none(user_id, rest[0])
        elif len(rest) == 3:
            cmd_set(user_id, rest[0], rest[1], rest[2])
        elif len(rest) == 2:
            cmd_set(user_id, rest[0], rest[1], None)
        else:
            raise SystemExit(__doc__)
    elif command == "forget":
        cmd_forget(user_id, *rest)
    elif command == "clear":
        cmd_clear(user_id)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
