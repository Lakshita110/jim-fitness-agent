from datetime import date

from jim.playbook import Playbook, load_playbook, template_prescription, use_existing_workout
from jim.schemas import ExerciseStep, StructuredSession


def session(**overrides) -> StructuredSession:
    base = dict(
        for_date=date(2026, 7, 9), kind="strength", title="Full Body A",
        est_duration_min=60,
    )
    base.update(overrides)
    return StructuredSession(**base)


def test_loads_real_playbook_files():
    pb = load_playbook()
    assert pb.rotation == ["full_body_a", "full_body_b", "full_body_c"]
    assert set(pb.workouts) == {"full_body_a", "full_body_b", "full_body_c"}
    assert set(pb.pt_routines) == {"pt_home", "pt_gym"}


def test_base_workouts_carry_garmin_ids():
    pb = load_playbook()
    assert pb.workouts["full_body_a"].garmin_workout_id == "1414012813"
    assert pb.workouts["full_body_b"].garmin_workout_id == "1414015802"
    assert pb.workouts["full_body_c"].garmin_workout_id == "1414019198"
    # both PT routines exist on Garmin with verified exercise enums
    assert pb.pt_routines["pt_home"].garmin_workout_id == "1625297181"
    assert pb.pt_routines["pt_gym"].garmin_workout_id == "1625297182"


def test_rotation_cycles():
    pb = load_playbook()
    assert pb.next_in_rotation(None) == "full_body_a"
    assert pb.next_in_rotation("full_body_a") == "full_body_b"
    assert pb.next_in_rotation("full_body_c") == "full_body_a"  # wraps
    assert pb.next_in_rotation("unknown") == "full_body_a"


def test_priority_and_flare_tags_preserved():
    pb = load_playbook()
    home = pb.pt_routines["pt_home"]
    all_ex = [e for b in home.blocks for e in b.exercises]
    eversion = next(e for e in all_ex if "eversion" in e.name.lower())
    assert "priority" in eversion.tags
    step_down = next(e for e in all_ex if "step-down" in e.name.lower())
    assert "skip_on_flare" in step_down.tags


def test_to_prompt_includes_ids_directives_and_doses():
    text = load_playbook().to_prompt()
    assert "garmin_workout_id=1414012813" in text
    assert "Full Body A" in text
    assert "Standing directives" in text
    assert "2/10 ceiling" in text  # directive content made it in
    # editing HTML comments are stripped
    assert "<!--" not in text


def test_empty_playbook_prompt_is_safe():
    assert Playbook().to_prompt().startswith("## Base strength rotation")


# --- template pick vs. adaptation (what actually reaches the watch) -----------


def test_template_with_no_steps_schedules_the_existing_workout():
    pb = load_playbook()
    s = session(garmin_workout_id="1414012813", template_key="full_body_a", steps=[])
    assert use_existing_workout(s, pb) is True


def test_adapted_day_is_rebuilt_even_when_it_echoes_the_template_id():
    """The bug: the model returns custom steps AND the template's Garmin ID, and
    the athlete's edits were silently dropped in favour of stock Full Body A."""
    pb = load_playbook()
    s = session(
        garmin_workout_id="1414012813", template_key="full_body_a",
        steps=[
            ExerciseStep(exercise="Goblet squat", sets=3, reps=12),
            ExerciseStep(exercise="Bulgarian split squat", sets=3, reps=8),  # not in the template
        ],
    )
    assert use_existing_workout(s, pb) is False


def test_prescribed_weight_counts_as_an_adaptation():
    pb = load_playbook()
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
    pb = load_playbook()
    steps = [
        ExerciseStep(exercise=name, sets=sets, reps=reps, duration_sec=secs)
        for name, sets, reps, secs in template_prescription(pb.workouts["full_body_a"])
    ]
    s = session(garmin_workout_id="1414012813", template_key="full_body_a", steps=steps)
    assert use_existing_workout(s, pb) is True


def test_custom_day_without_a_template_id_is_always_built():
    pb = load_playbook()
    s = session(steps=[ExerciseStep(exercise="Bench press", sets=3, reps=8)])
    assert use_existing_workout(s, pb) is False
