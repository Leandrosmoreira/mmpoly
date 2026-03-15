"""Tests for BUG-033..036: Adverse sell improvements.

BUG-033: FOK spam loop — retry limit + cooldown + POST_ONLY fallback
BUG-034: No sell price floor — max_loss_per_share limits catastrophic sells
BUG-035: No post-adverse cooldown — 60s cooldown prevents re-entry
BUG-036: buy_blocked not persisted — snapshot saves/restores side tracking
"""

import json
import time
import tempfile
from pathlib import Path

import pytest

from core.types import (
    BotConfig, BotState, Direction, Inventory, IntentType,
    MarketState, Side, TimeRegime, TopOfBook,
)
from core.engine import Engine
from data.inventory import InventoryTracker
from execution.order_manager import OrderManager


def _make_cfg(**overrides) -> BotConfig:
    defaults = dict(
        adverse_loss_threshold=-0.50,
        adverse_sell_at_bid=True,
        adverse_max_fok_attempts=3,
        adverse_cooldown_s=60.0,
        adverse_max_loss_per_share=0.12,
        t_early=600, t_mid=120, t_late=30, t_exit=15,
        min_buy_price=0.15,
        dry_run=True,
    )
    defaults.update(overrides)
    return BotConfig(**defaults)


def _make_market(
    inv=None, state=BotState.QUOTING,
    book_up_bid=0.50, book_up_ask=0.52,
    book_down_bid=0.48, book_down_ask=0.50,
) -> MarketState:
    return MarketState(
        name="test-market",
        condition_id="cond-123",
        token_up="tok-up",
        token_down="tok-down",
        book_up=TopOfBook(
            token_id="tok-up", best_bid=book_up_bid, best_ask=book_up_ask,
            best_bid_sz=100, best_ask_sz=100, ts=time.time(),
        ),
        book_down=TopOfBook(
            token_id="tok-down", best_bid=book_down_bid, best_ask=book_down_ask,
            best_bid_sz=100, best_ask_sz=100, ts=time.time(),
        ),
        inventory=inv or Inventory(),
        state=state,
        end_ts=time.time() + 600,
    )


def _make_order_mgr():
    return OrderManager(BotConfig(dry_run=True))


# ============================================================
# BUG-033: FOK spam loop — retry limit + POST_ONLY fallback
# ============================================================

class TestAdverseFokRetryLimit:
    """BUG-033: After max FOK attempts, engine switches to POST_ONLY."""

    def test_first_attempt_uses_fok_reason(self):
        """First adverse sell should use FOK reason (adverse_sell_*)."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_max_fok_attempts=3)
        engine = Engine(market, cfg)

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        assert sells[0].reason == "adverse_sell_up"
        assert engine._adverse_fok_attempts == 1

    def test_switches_to_postonly_after_max_attempts(self):
        """After 3 failed FOK attempts, reason switches to adverse_sell_postonly_*."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_max_fok_attempts=3, adverse_cooldown_s=0.01)
        engine = Engine(market, cfg)

        # Simulate 3 FOK failures
        engine._adverse_fok_attempts = 3
        engine._adverse_cooldown_until = 0  # Reset cooldown for test

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        # 4th attempt (> 3) → postonly
        assert sells[0].reason == "adverse_sell_postonly_up"
        assert engine._adverse_fok_attempts == 4

    def test_cooldown_blocks_retry(self):
        """After cooldown is set, adverse sell should not fire."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_cooldown_s=60.0)
        engine = Engine(market, cfg)

        # Set cooldown in the future
        engine._adverse_cooldown_until = time.time() + 30.0

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) == 0, "Should be blocked by adverse cooldown"

    def test_attempt_counter_increments(self):
        """Each adverse tick increments the FOK attempt counter."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_cooldown_s=0.01)  # Tiny cooldown for test
        engine = Engine(market, cfg)

        # First tick
        engine.tick([], _make_order_mgr())
        assert engine._adverse_fok_attempts == 1

        # Reset cooldowns for next tick
        engine._adverse_cooldown_until = 0
        engine.market.cooldown_until = 0

        # Second tick
        engine.tick([], _make_order_mgr())
        assert engine._adverse_fok_attempts == 2


# ============================================================
# BUG-034: No sell price floor
# ============================================================

class TestAdverseSellPriceFloor:
    """BUG-034: Emergency sells should have a price floor."""

    def test_price_floored_at_max_loss(self):
        """Sell price clamped to avg_cost - max_loss_per_share."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.55)
        # Bid crashed to 0.30, but floor = 0.55 - 0.12 = 0.43
        market = _make_market(inv=inv, book_up_bid=0.30, book_up_ask=0.32)
        cfg = _make_cfg(adverse_max_loss_per_share=0.12)
        engine = Engine(market, cfg)

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        assert sells[0].price == 0.43  # 0.55 - 0.12 = 0.43

    def test_no_floor_when_bid_above_limit(self):
        """When bid is above floor, use bid directly."""
        # Need loss > $0.50 to trigger adverse. 10 shares * (0.48-0.60) = -$1.20
        inv = Inventory(shares_up=10.0, avg_cost_up=0.60)
        # Bid at 0.48, floor = 0.60 - 0.12 = 0.48. Bid == floor, use bid.
        market = _make_market(inv=inv, book_up_bid=0.49, book_up_ask=0.51)
        cfg = _make_cfg(adverse_max_loss_per_share=0.12, net_hard_limit=25)
        engine = Engine(market, cfg)

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        assert sells[0].price == 0.49  # Bid is above floor, uses bid directly

    def test_price_floor_down_side(self):
        """Price floor also works for DOWN side."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.60)
        # DOWN bid crashed to 0.35, floor = 0.60 - 0.12 = 0.48
        market = _make_market(inv=inv, book_down_bid=0.35, book_down_ask=0.37)
        cfg = _make_cfg(adverse_max_loss_per_share=0.12)
        engine = Engine(market, cfg)

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        assert sells[0].price == 0.48  # 0.60 - 0.12 = 0.48

    def test_catastrophic_loss_prevented(self):
        """Would have been -$1.20 loss, now limited to -$0.60."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        # Bid crashed to 0.36 (real production scenario)
        market = _make_market(inv=inv, book_up_bid=0.36, book_up_ask=0.38)
        cfg = _make_cfg(adverse_max_loss_per_share=0.12)
        engine = Engine(market, cfg)

        intents = engine.tick([], _make_order_mgr())
        sells = [i for i in intents if i.reason.startswith("adverse_sell")]
        assert len(sells) > 0
        # Floor: 0.60 - 0.12 = 0.48
        assert sells[0].price == 0.48
        # Max loss: (0.48 - 0.60) * 5 = -$0.60 vs old (0.36 - 0.60) * 5 = -$1.20
        max_loss = (sells[0].price - inv.avg_cost_up) * inv.shares_up
        assert max_loss >= -0.61  # ~-$0.60


# ============================================================
# BUG-035: No post-adverse cooldown
# ============================================================

class TestPostAdverseCooldown:
    """BUG-035: After adverse sell, market gets cooldown to prevent re-entry."""

    def test_cooldown_set_after_adverse(self):
        """Market cooldown should be set after adverse sell."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_cooldown_s=60.0)
        engine = Engine(market, cfg)

        before = time.time()
        engine.tick([], _make_order_mgr())

        # Market cooldown should be set ~60s in the future
        assert engine.market.cooldown_until > before + 55

    def test_normal_quoting_blocked_during_cooldown(self):
        """After adverse sell, normal quoting should be blocked by cooldown."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        market = _make_market(inv=inv, book_up_bid=0.45, book_up_ask=0.47)
        cfg = _make_cfg(adverse_cooldown_s=60.0)
        engine = Engine(market, cfg)

        # First tick: adverse sell fires
        intents1 = engine.tick([], _make_order_mgr())
        assert any(i.reason.startswith("adverse_sell") for i in intents1)

        # Reset inventory (simulate sell filled)
        engine.market.inventory = Inventory()

        # Second tick: should be blocked by cooldown
        intents2 = engine.tick([], _make_order_mgr())
        assert len(intents2) == 0, "Cooldown should block all activity"


# ============================================================
# BUG-036: buy_blocked not persisted in snapshot
# ============================================================

class TestBuyBlockedPersistence:
    """BUG-036: side_realized and buy_blocked should survive restart."""

    def test_save_and_load_buy_blocked(self):
        """buy_blocked flags should be saved and restored."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            # Create tracker with loss tracking
            tracker = InventoryTracker(snapshot_path=path)
            inv = tracker.get("test-market")
            inv.shares_up = 5.0
            inv.avg_cost_up = 0.55
            inv.side_realized_up = -0.30
            inv.buy_blocked_up = True
            inv.side_realized_down = -0.15
            inv.buy_blocked_down = True
            tracker._save_snapshot()

            # New tracker, load from snapshot
            tracker2 = InventoryTracker(snapshot_path=path)
            tracker2.load_snapshot(max_age_s=60)
            inv2 = tracker2.get("test-market")

            assert inv2.side_realized_up == -0.30
            assert inv2.buy_blocked_up is True
            assert inv2.side_realized_down == -0.15
            assert inv2.buy_blocked_down is True
        finally:
            Path(path).unlink(missing_ok=True)

    def test_snapshot_without_buy_blocked_loads_defaults(self):
        """Old snapshots without buy_blocked fields should load with defaults."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            # Old-format snapshot (no buy_blocked fields)
            json.dump({
                "ts": time.time(),
                "markets": {
                    "test-market": {
                        "shares_up": 5.0,
                        "shares_down": 0.0,
                        "avg_cost_up": 0.50,
                        "avg_cost_down": 0.0,
                        "realized_pnl": -0.10,
                    }
                }
            }, f)
            path = f.name

        try:
            tracker = InventoryTracker(snapshot_path=path)
            tracker.load_snapshot(max_age_s=60)
            inv = tracker.get("test-market")

            # Should load with defaults (not blocked)
            assert inv.shares_up == 5.0
            assert inv.side_realized_up == 0
            assert inv.buy_blocked_up is False
            assert inv.buy_blocked_down is False
        finally:
            Path(path).unlink(missing_ok=True)

    def test_buy_blocked_survives_fill_cycle(self):
        """buy_blocked set by fill should persist through save/load."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            tracker = InventoryTracker(snapshot_path=path)
            inv = tracker.get("test-market")

            # Buy UP at 0.55, then sell at 0.40 (loss of -$0.75)
            from core.types import Fill
            buy_fill = Fill(
                order_id="buy-1", market_name="test-market",
                token_id="tok-up", side=Side.UP,
                direction=Direction.BUY, price=0.55, size=5.0, ts=time.time(),
            )
            tracker.apply_fill(buy_fill)

            sell_fill = Fill(
                order_id="sell-1", market_name="test-market",
                token_id="tok-up", side=Side.UP,
                direction=Direction.SELL, price=0.40, size=5.0, ts=time.time(),
            )
            tracker.apply_fill(sell_fill)

            # Should be blocked now
            inv = tracker.get("test-market")
            assert inv.buy_blocked_up is True
            assert inv.side_realized_up == pytest.approx(-0.75, abs=0.01)

            # Load in new tracker
            tracker2 = InventoryTracker(snapshot_path=path)
            tracker2.load_snapshot(max_age_s=60)
            inv2 = tracker2.get("test-market")

            assert inv2.buy_blocked_up is True
            assert inv2.side_realized_up == pytest.approx(-0.75, abs=0.01)
        finally:
            Path(path).unlink(missing_ok=True)
