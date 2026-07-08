from datetime import date

from jim.agent.heuristics import something_off, state_ambiguous
from jim.schemas import GarminToday, HistoryFeatures, NotionDay

DAY = date(2026, 7, 6)


def garmin(**kw) -> GarminToday:
    return GarminToday(day=DAY, **kw)


def notion(**kw) -> NotionDay:
    return NotionDay(day=DAY, **kw)


def feats(**kw) -> HistoryFeatures:
    base = {"as_of": DAY, "window_days": 28}
    base.update(kw)
    return HistoryFeatures(**base)


def test_routine_night_is_quiet():
    reasons = something_off(
        garmin(readiness=70, body_battery=60), notion(pain_level=1, pt_done=True), feats()
    )
    assert reasons == []


def test_pain_spike_fires():
    reasons = something_off(garmin(), notion(pain_level=6, pt_done=True), feats())
    assert any("pain spike" in r for r in reasons)


def test_pain_trend_fires():
    reasons = something_off(garmin(), notion(pt_done=True), feats(pain_trend=0.3))
    assert any("trending up" in r for r in reasons)


def test_low_readiness_and_battery_fire():
    reasons = something_off(
        garmin(readiness=20, body_battery=10), notion(pt_done=True), feats()
    )
    assert len(reasons) == 2


def test_skipped_pt_on_painful_day_fires():
    reasons = something_off(garmin(), notion(pain_level=3, pt_done=False), feats())
    assert any("PT skipped" in r for r in reasons)


def test_escalation_requires_multiple_or_conflicting_signals():
    assert not state_ambiguous([], feats())
    assert not state_ambiguous(["one flag"], feats(days_since_legs=1))
    assert state_ambiguous(["a", "b"], feats())
    assert state_ambiguous(["pain trending up"], feats(days_since_legs=10))
