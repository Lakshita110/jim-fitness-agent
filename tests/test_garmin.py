"""Garmin field-mapping tests — pure dict parsing, no live API."""

from datetime import date

import jim.tools.garmin as garmin_mod
from jim.tools.garmin import body_battery_recovered, get_scheduled_workouts

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


# --- get_scheduled_workouts --------------------------------------------------


class _FakeClient:
    """Records (year, month) calls and returns whatever's queued for that key."""

    def __init__(self, by_month: dict[tuple[int, int], list[dict]]):
        self.by_month = by_month
        self.calls: list[tuple[int, int]] = []

    def get_scheduled_workouts(self, year, month):
        self.calls.append((year, month))
        return {"calendarItems": self.by_month.get((year, month), [])}


def _use(monkeypatch, fake):
    monkeypatch.setattr(garmin_mod, "client", lambda user_id: fake)


def test_get_scheduled_workouts_filters_to_workout_items_in_range(monkeypatch):
    fake = _FakeClient({
        (2026, 7): [
            {"itemType": "workout", "id": 1, "date": "2026-07-15",
             "title": "Full Body A (modified)", "workoutId": 555},
            {"itemType": "healthEvent", "id": 2, "date": "2026-07-16",
             "title": "irrelevant"},  # decoy: not a workout, no workoutId
            {"itemType": "workout", "id": 3, "date": "2026-07-17",
             "title": "one-off"},  # decoy: workout but missing workoutId
        ]
    })
    _use(monkeypatch, fake)
    out = get_scheduled_workouts(1, date(2026, 7, 15), date(2026, 7, 21))
    assert out == [{"date": date(2026, 7, 15), "workout_id": "555",
                     "title": "Full Body A (modified)"}]


def test_get_scheduled_workouts_spans_two_calendar_months(monkeypatch):
    fake = _FakeClient({
        (2026, 7): [{"itemType": "workout", "id": 1, "date": "2026-07-30",
                      "title": "PT", "workoutId": 1}],
        (2026, 8): [{"itemType": "workout", "id": 2, "date": "2026-08-02",
                      "title": "Lift", "workoutId": 2}],
    })
    _use(monkeypatch, fake)
    out = get_scheduled_workouts(1, date(2026, 7, 28), date(2026, 8, 3))
    assert fake.calls == [(2026, 7), (2026, 8)]
    assert [o["date"] for o in out] == [date(2026, 7, 30), date(2026, 8, 2)]


def test_get_scheduled_workouts_returns_workout_id_as_str(monkeypatch):
    fake = _FakeClient({
        (2026, 7): [{"itemType": "workout", "id": 1, "date": "2026-07-15",
                      "title": "Lift", "workoutId": 555}],
    })
    _use(monkeypatch, fake)
    out = get_scheduled_workouts(1, date(2026, 7, 15), date(2026, 7, 15))
    assert isinstance(out[0]["workout_id"], str)
    assert out[0]["workout_id"] == "555"
