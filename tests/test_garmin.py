"""Garmin field-mapping tests — pure dict parsing, no live API."""

from jim.tools.garmin import body_battery_recovered

# --- body battery as a recovery read ---------------------------------------


def test_body_battery_prefers_the_wake_value():
    """Body battery drains all day: "most recent" is the bedtime dregs (5), not
    recovery (75). Reading the drained value pushed ~84% of days under the
    poor-recovery threshold, so the coach prescribed rest nearly every day."""
    stats = {
        "bodyBatteryAtWakeTime": 75,
        "bodyBatteryHighestValue": 81,
        "bodyBatteryMostRecentValue": 5,
    }
    assert body_battery_recovered(stats) == 75


def test_body_battery_falls_back_through_peak_then_most_recent():
    assert body_battery_recovered(
        {"bodyBatteryHighestValue": 81, "bodyBatteryMostRecentValue": 5}
    ) == 81
    assert body_battery_recovered({"bodyBatteryMostRecentValue": 40}) == 40
    assert body_battery_recovered({}) is None


def test_body_battery_keeps_a_legitimate_zero():
    """0 is a real reading — a falsy-check would discard it and fall through."""
    assert body_battery_recovered(
        {"bodyBatteryAtWakeTime": 0, "bodyBatteryHighestValue": 60}
    ) == 0
