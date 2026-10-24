"""
Гейт змін перед LLM-викликом (2026-10-24): той самий висновок не
робиться вдруге на тих самих входах — але й не застаріває тихо.
"""

from datetime import datetime, timedelta, timezone

from change_gate import Anchor, compare, fingerprint

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
STABLE = {"factors": [{"key": "eia:crude_stocks", "last": ["2026-10-16", "421.3"]}]}


def _previous(stable=STABLE, anchors=None, days_ago=1):
    return {
        "fingerprint": fingerprint(stable),
        "anchors": {"price": 100.0} if anchors is None else anchors,
        "updated_at": NOW - timedelta(days=days_ago),
    }


def test_first_run_is_always_changed():
    assert compare(None, STABLE, {}, now=NOW).changed


def test_same_inputs_are_unchanged():
    decision = compare(_previous(), STABLE, {"price": Anchor(100.5, 1.0)}, now=NOW)
    assert not decision.changed
    assert decision.since == NOW - timedelta(days=1)


def test_fingerprint_ignores_key_order():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})


def test_new_release_opens_gate():
    """Рідкий ряд (запаси щотижня): нова точка = нові дані."""
    new = {"factors": [{"key": "eia:crude_stocks", "last": ["2026-10-23", "418.0"]}]}
    decision = compare(_previous(), new, {"price": Anchor(100.0, 1.0)}, now=NOW)
    assert decision.changed
    assert "нові дані" in decision.reason


def test_relative_anchor_beyond_tolerance_opens_gate():
    decision = compare(_previous(), STABLE, {"price": Anchor(101.5, 1.0)}, now=NOW)
    assert decision.changed
    assert "price" in decision.reason


def test_drift_is_measured_from_last_analysis_not_yesterday():
    """Порівняння з ціною МИНУЛОГО аналізу: три дні по 0.5% — це вже 1.5%,
    і гейт відкривається, хоча жоден день окремо поріг не перетнув."""
    previous = _previous(anchors={"price": 100.0})
    assert not compare(previous, STABLE, {"price": Anchor(100.5, 1.0)}, now=NOW).changed
    assert compare(previous, STABLE, {"price": Anchor(101.51, 1.0)}, now=NOW).changed


def test_absolute_anchor_for_yields():
    previous = _previous(anchors={"10y": 4.20})
    assert not compare(previous, STABLE, {"10y": Anchor(4.27, 0.10, relative=False)}, now=NOW).changed
    assert compare(previous, STABLE, {"10y": Anchor(4.31, 0.10, relative=False)}, now=NOW).changed


def test_new_anchor_opens_gate():
    """Число, якого не було минулого разу (нове джерело ціни) — нові дані."""
    decision = compare(_previous(anchors={}), STABLE, {"price": Anchor(100.0, 1.0)}, now=NOW)
    assert decision.changed


def test_conclusion_is_refreshed_after_max_age():
    decision = compare(_previous(days_ago=7), STABLE, {"price": Anchor(100.0, 1.0)}, now=NOW, max_age_days=7)
    assert decision.changed
    assert "примусово" in decision.reason
