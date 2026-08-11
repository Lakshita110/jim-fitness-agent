from datetime import date

import pytest

from jim.playbook import (
    Block,
    Exercise,
    Playbook,
    _load_default_playbook,
    _load_playbook_from_disk,
    save_workout_template,
    set_rotation,
    template_prescription,
    use_existing_workout,
)
from jim.schemas import ExerciseStep, StructuredSession


def session(**overrides) -> StructuredSession:
    base = dict(
        for_date=date(2026, 7, 9), kind="strength", title="Full Body A",
        est_duration_min=60,
    )
    base.update(overrides)
    return StructuredSession(**base)


def test_loads_real_playbook_files():
    pb = _load_playbook_from_disk()
    assert pb.rotation == ["full_body_a", "full_body_b", "full_body_c"]
    assert set(pb.workouts) == {
        "full_body_a", "full_body_b", "full_body_c", "pt_home", "pt_gym",
    }


def test_base_workouts_carry_garmin_ids():
    pb = _load_playbook_from_disk()
    assert pb.workouts["full_body_a"].garmin_workout_id == "1414012813"
    assert pb.workouts["full_body_b"].garmin_workout_id == "1414015802"
    assert pb.workouts["full_body_c"].garmin_workout_id == "1414019198"
    # both PT routines exist on Garmin with verified exercise enums
    assert pb.workouts["pt_home"].garmin_workout_id == "1625297181"
    assert pb.workouts["pt_gym"].garmin_workout_id == "1625297182"


def test_rotation_from_gives_the_whole_continuing_order():
    """The coach plans a week in one turn, so it needs the full order from
    here — not just the next key."""
    pb = _load_playbook_from_disk()
    a, b, c = "full_body_a", "full_body_b", "full_body_c"
    assert pb.rotation_from(None) == [a, b, c]
    assert pb.rotation_from(a) == [b, c, a]
    assert pb.rotation_from(c) == [a, b, c]          # wraps
    assert pb.rotation_from("unknown") == [a, b, c]  # dropped template can't pin it
    assert Playbook().rotation_from(a) == []


def test_priority_and_flare_tags_preserved():
    pb = _load_playbook_from_disk()
    home = pb.workouts["pt_home"]
    all_ex = [e for b in home.blocks for e in b.exercises]
    eversion = next(e for e in all_ex if "eversion" in e.name.lower())
    assert "priority" in eversion.tags
    step_down = next(e for e in all_ex if "step-down" in e.name.lower())
    assert "skip_on_flare" in step_down.tags


def test_to_prompt_includes_ids_directives_and_doses():
    text = _load_playbook_from_disk().to_prompt()
    assert "garmin_workout_id=1414012813" in text
    assert "Full Body A" in text
    assert "Standing directives" in text
    assert "2/10 ceiling" in text  # directive content made it in
    # editing HTML comments are stripped
    assert "<!--" not in text


def test_empty_playbook_prompt_is_safe():
    assert Playbook().to_prompt().startswith("## Rotation")


def test_default_playbook_seed_is_generic_not_the_real_athletes_content():
    default = _load_default_playbook()
    assert default.rotation == []
    assert default.workouts == {}
    assert "Settings" in default.directives and "Playbook" in default.directives
    real = _load_playbook_from_disk()
    assert default.directives != real.directives
    assert default.rotation != real.rotation


# --- template pick vs. adaptation (what actually reaches the watch) -----------


def test_template_with_no_steps_schedules_the_existing_workout():
    pb = _load_playbook_from_disk()
    s = session(garmin_workout_id="1414012813", template_key="full_body_a", steps=[])
    assert use_existing_workout(s, pb) is True


def test_adapted_day_is_rebuilt_even_when_it_echoes_the_template_id():
    """The bug: the model returns custom steps AND the template's Garmin ID, and
    the athlete's edits were silently dropped in favour of stock Full Body A."""
    pb = _load_playbook_from_disk()
    s = session(
        garmin_workout_id="1414012813", template_key="full_body_a",
        steps=[
            ExerciseStep(exercise="Goblet squat", sets=3, reps=12),
            ExerciseStep(exercise="Bulgarian split squat", sets=3, reps=8),  # not in the template
        ],
    )
    assert use_existing_workout(s, pb) is False


def test_prescribed_weight_counts_as_an_adaptation():
    pb = _load_playbook_from_disk()
    unchanged = template_prescription(pb.workouts["full_body_a"])
    steps = [
        ExerciseStep(exercise=name, sets=sets, reps=reps, duration_sec=secs)
        for name, sets, reps, secs in unchanged
    ]
    steps[3] = steps[3].model_copy(update={"weight_kg": 20.0})  # "bump goblet squats to 20kg"
    s = session(garmin_workout_id="1414012813", template_key="full_body_a", steps=steps)
    assert use_existing_workout(s, pb) is False


def test_verbatim_echo_of_the_template_still_schedules_by_id():
    """A model that restates the template instead of leaving steps empty must not
    cost the athlete the weights loaded on the Garmin workout."""
    pb = _load_playbook_from_disk()
    steps = [
        ExerciseStep(exercise=name, sets=sets, reps=reps, duration_sec=secs)
        for name, sets, reps, secs in template_prescription(pb.workouts["full_body_a"])
    ]
    s = session(garmin_workout_id="1414012813", template_key="full_body_a", steps=steps)
    assert use_existing_workout(s, pb) is True


def test_a_fabricated_template_pick_is_never_trusted():
    """Caught testing live: a model invented an entire template ("PT Day ·
    Gym") that wasn't in the account's actual playbook, with a plausible ID
    and no steps — the empty-steps contract used to trust that unconditionally.
    An ID/key that doesn't resolve against THIS playbook must never be
    scheduled, empty steps or not."""
    pb = _load_playbook_from_disk()  # only full_body_a/b/c + pt_home/pt_gym exist
    s = session(garmin_workout_id="9999999999", template_key="upper_x", steps=[])
    assert use_existing_workout(s, pb) is False


def test_custom_day_without_a_template_id_is_always_built():
    pb = _load_playbook_from_disk()
    s = session(steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8)])
    assert use_existing_workout(s, pb) is False


# --- authoring the playbook (save_workout_template / set_rotation) ------------


@pytest.fixture
def store(monkeypatch):
    """Playbook persistence swapped for a dict, so the writers are testable
    without Postgres."""
    data = {1: _load_playbook_from_disk()}
    monkeypatch.setattr("jim.playbook.load_playbook", lambda uid: data[uid])
    monkeypatch.setattr("jim.playbook.save_playbook", lambda uid, pb: data.__setitem__(uid, pb))
    return data


def test_save_workout_template_edits_in_place(store):
    updated = save_workout_template(
        1, "full_body_a", label="Full Body A (v2)",
        warmup=[Exercise(name="Jumping jacks", reps=20)],
    )

    assert updated.label == "Full Body A (v2)"
    assert updated.warmup == [Exercise(name="Jumping jacks", reps=20)]
    # unspecified fields (blocks/equipment) are left untouched
    assert updated.blocks == _load_playbook_from_disk().workouts["full_body_a"].blocks
    assert store[1].workouts["full_body_a"].label == "Full Body A (v2)"


def test_changing_steps_clears_the_garmin_id_but_a_rename_does_not(store):
    """Garmin has no update-workout API, so changed steps must be rebuilt. A
    rename changes nothing on the watch — dropping the ID there would throw
    away the weights the athlete has loaded onto that workout."""
    renamed = save_workout_template(1, "full_body_a", label="Leg Day")
    assert renamed.garmin_workout_id == "1414012813"

    rewritten = save_workout_template(
        1, "full_body_a", blocks=[Block(exercises=[Exercise(name="Goblet squat", reps=8)])],
    )
    assert rewritten.garmin_workout_id is None


def test_save_workout_template_creates_a_new_workout(store):
    created = save_workout_template(
        1, "upper_a", label="Upper A", sport="strength_training",
        blocks=[Block(exercises=[Exercise(name="Push-up", reps=10)])],
    )

    assert created.key == "upper_a"
    assert created.label == "Upper A"
    assert created.garmin_workout_id is None  # nothing on Garmin yet
    assert store[1].workouts["upper_a"].sport == "strength_training"
    # creating a workout doesn't silently put it in the rotation
    assert "upper_a" not in store[1].rotation


def test_creating_without_label_or_sport_is_rejected(store):
    with pytest.raises(RuntimeError, match="needs label and sport"):
        save_workout_template(1, "upper_a")
    with pytest.raises(RuntimeError, match="needs sport"):
        save_workout_template(1, "upper_a", label="Upper A")
    assert "upper_a" not in store[1].workouts


def test_set_rotation_replaces_the_order(store):
    assert set_rotation(1, ["full_body_c", "pt_home", "full_body_a"]) == [
        "full_body_c", "pt_home", "full_body_a",
    ]
    assert store[1].rotation == ["full_body_c", "pt_home", "full_body_a"]
    # a rotation is an order, not a set — repeats are legitimate
    assert set_rotation(1, ["full_body_a", "pt_home", "full_body_a"]) == [
        "full_body_a", "pt_home", "full_body_a",
    ]
    assert set_rotation(1, []) == []


def test_set_rotation_rejects_keys_with_no_workout(store):
    before = list(store[1].rotation)
    with pytest.raises(RuntimeError, match="no playbook workout with key 'nope'"):
        set_rotation(1, ["full_body_a", "nope"])
    assert store[1].rotation == before  # all-or-nothing
