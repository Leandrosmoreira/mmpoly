"""BUG-027: Inventory skew should NOT make buys more aggressive.

Previously, when heavy on DOWN (net < 0), inventory correction
returned positive → bid_adj positive → buys MORE aggressive.
This is exactly wrong during a crash — the bot buys more as price drops.

Fix: Inventory correction only affects the ASK side (sell faster).
The bid side uses directional signal only.
"""

import pytest
from core.types import SkewConfig
from core.skew import SkewEngine


def _make_engine(**overrides) -> SkewEngine:
    cfg = SkewConfig(enabled=True, shadow_mode=False)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return SkewEngine(cfg)


class TestSkewBidFix:
    """BUG-027: bid_adj should not include inventory correction."""

    def test_heavy_down_bid_not_aggressive(self):
        """When heavy DOWN (net=-10), bid_adj should NOT be positive.

        Before fix: inv_component=+1.0, bid_adj = dir + inv = positive (more aggressive buys)
        After fix:  bid_adj = dir only (no inv), ask_adj = dir - inv (more aggressive sells)
        """
        engine = _make_engine()
        # inv_component when net=-10, soft_limit=10: _inventory(-10, 10) = clamp(10/10) = 1.0
        inv_c = 1.0  # heavy DOWN → positive component
        scaled = 0.0  # no directional signal

        bid_adj, ask_adj = engine._side_adjustments(scaled, inv_c)

        # BUG-027: bid should NOT get inventory correction
        assert bid_adj == 0.0  # direction is 0, inventory excluded from bid
        # ask should get full inventory correction (sell faster)
        assert ask_adj < 0  # negative = lower ask = sell more aggressively

    def test_heavy_up_bid_not_defensive(self):
        """When heavy UP (net=+10), bid_adj should stay directional only."""
        engine = _make_engine()
        inv_c = -1.0  # heavy UP → negative component
        scaled = 0.0

        bid_adj, ask_adj = engine._side_adjustments(scaled, inv_c)

        # bid: direction only (0), no inventory
        assert bid_adj == 0.0
        # ask: positive = raise ask = less aggressive sell (wait for better price)
        assert ask_adj > 0

    def test_directional_signal_still_affects_bid(self):
        """Directional signal (velocity/imbalance) still moves bid."""
        engine = _make_engine()
        inv_c = 0.0
        scaled = 0.5  # bullish signal

        bid_adj, ask_adj = engine._side_adjustments(scaled, inv_c)

        # Both should be affected by direction
        assert bid_adj > 0  # raise bid (buy more aggressively with trend)
        assert ask_adj > 0  # raise ask (sell less aggressively with trend)

    def test_ask_gets_both_direction_and_inventory(self):
        """Ask side gets both directional + inventory correction."""
        engine = _make_engine()
        inv_c = 1.0  # heavy DOWN
        scaled = 0.5  # bullish signal

        bid_adj, ask_adj = engine._side_adjustments(scaled, inv_c)

        # bid: direction only
        max_adj = engine.cfg.max_side_adj
        assert bid_adj == pytest.approx(0.5 * scaled * max_adj, abs=1e-6)
        # ask: direction - inventory → lower (more aggressive sell)
        assert ask_adj < bid_adj
