"""The semantic fallback for matching a movement to Garmin's exercise library.

`garmin.classify_garmin_exercise` matches on words, which fails in exactly one
way: when the athlete's vocabulary and Garmin's share none. "Copenhagen plank"
finds PLANK because they both say "plank"; "sissy squat", "Jefferson curl" and
"GHR" find nothing, and an unmatched step lands on the watch as a bare note.

A model knows those are the same movements Garmin files under other names, so it
gets the leftovers — and only the leftovers:

- **Lexical first.** The string matcher already handles everything in the
  playbook, so a push normally costs zero tokens. This runs on the misses.
- **One call per push**, not one per exercise: the whole session's unmatched
  names go in together.
- **Cached in kv, negatives included.** A novel name is paid for once, ever.
  Delete the `exercise_map` key to re-ask.
- **Validated against the library.** The model will confidently invent enums
  (CORE/SINGLE_LEG_CIRCLES was exactly that mistake, made by a human). Anything
  that isn't a real (category, exercise) pair is discarded — a described step is
  better than a wrong one.

It is a side effect, so it is injected: only `create_garmin_workout` reaches for
it, and the payload builders take a resolver they can be handed a fake for.
"""

import json
import logging
from collections.abc import Callable, Iterable

from jim.config import MODEL_FAST, OPENROUTER_BASE_URL
from jim.db import kv_get, kv_set
from jim.tools.garmin import _normalize, exercise_library

log = logging.getLogger(__name__)

CACHE_KEY = "exercise_map"

Pair = tuple[str, str | None]
Resolver = Callable[[list[str]], dict[str, Pair]]

SYSTEM_PROMPT = """You map strength & rehab movements onto Garmin's fixed exercise
taxonomy. You are given the taxonomy and a list of movement names that a lexical
matcher could not place.

For each name, pick the CLOSEST exercise Garmin actually has — same movement
pattern, same working muscles, same joint action. Equipment differences are fine
(a barbell variant is an acceptable match for a bodyweight one). A different
movement is NOT: do not map a hinge to a squat to fill a slot.

If Garmin has nothing close, return null for that name. A step with no match is
described in plain text on the watch, which is fine — a WRONG exercise is not.

Use ONLY category and exerciseName values that appear in the taxonomy verbatim,
and the exerciseName MUST be listed under the category you pair it with.

Respond with a single JSON object, one key per name you were given:
{"<name>": {"category": "SQUAT", "exerciseName": "GOBLET_SQUAT"}, "<name>": null}"""


def taxonomy_prompt() -> str:
    """Garmin's whole library, grouped by category, for the model to choose from."""
    by_category: dict[str, list[str]] = {}
    for category, exercise, _, _ in exercise_library():
        by_category.setdefault(category, []).append(exercise)
    return "\n".join(
        f"{category}: {', '.join(sorted(exercises))}"
        for category, exercises in sorted(by_category.items())
    )


def valid_pairs() -> dict[str, set[str]]:
    pairs: dict[str, set[str]] = {}
    for category, exercise, _, _ in exercise_library():
        pairs.setdefault(category, set()).add(exercise)
    return pairs


def llm_match(names: Iterable[str], model: str = MODEL_FAST) -> dict[str, Pair]:
    """Ask the model to place `names` in the taxonomy. Invalid answers dropped."""
    wanted = list(names)
    if not wanted:
        return {}

    from openai import OpenAI

    from jim.config import settings

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=settings().openrouter_api_key)
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"# GARMIN TAXONOMY\n{taxonomy_prompt()}\n\n"
                    f"# MOVEMENTS TO PLACE\n" + "\n".join(f"- {n}" for n in wanted)
                ),
            },
        ],
    )
    raw = json.loads(resp.choices[0].message.content or "{}")

    allowed = valid_pairs()
    matched: dict[str, Pair] = {}
    for name in wanted:
        answer = raw.get(name)
        if not isinstance(answer, dict):
            continue
        category, exercise = answer.get("category"), answer.get("exerciseName")
        if category not in allowed or (exercise is not None and exercise not in allowed[category]):
            log.warning("exercise match: discarding invented pair %s/%s for %r",
                        category, exercise, name)
            continue
        matched[name] = (category, exercise)
    return matched


def semantic_resolver(model: str = MODEL_FAST) -> Resolver:
    """A resolver that reads the kv cache first and asks the model for the rest."""

    def resolve(names: list[str]) -> dict[str, Pair]:
        cache: dict = kv_get(CACHE_KEY) or {}
        resolved: dict[str, Pair] = {}
        unknown: list[str] = []

        for name in names:
            key = _normalize(name)
            if key in cache:
                hit = cache[key]
                if hit:  # a cached null means "Garmin has nothing" — don't re-ask
                    resolved[name] = (hit[0], hit[1])
            else:
                unknown.append(name)

        if unknown:
            fresh = llm_match(unknown, model)
            for name in unknown:
                pair = fresh.get(name)
                cache[_normalize(name)] = list(pair) if pair else None
                if pair:
                    resolved[name] = pair
            kv_set(CACHE_KEY, cache)
            log.info("exercise match: resolved %d/%d unmatched movements via %s",
                     len(fresh), len(unknown), model)

        return resolved

    return resolve
