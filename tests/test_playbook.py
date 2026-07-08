from jim.playbook import Playbook, load_playbook


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
