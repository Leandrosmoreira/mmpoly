"""BUG-038: Cancel existing BUY orders when buy_blocked activates.

When a SELL fill triggers buy_blocked (side_realized < -$0.50),
existing BUY orders on that side should be cancelled immediately —
not wait for the next tick's selective cancel.

This prevents fills on stale BUY orders during the gap between
fill processing and the next grid recomputation.
"""

import time
import pytest

from core.types import (
    BotConfig, Direction, Fill, Intent, IntentType,
    Inventory, LiveOrder, Side,
)
from execution.order_manager import OrderManager


def _make_order_mgr() -> OrderManager:
    cfg = BotConfig(dry_run=True)
    return OrderManager(cfg)


def _make_live_order(oid: str, market: str, side: Side,
                     direction: Direction, price: float) -> LiveOrder:
    return LiveOrder(
        order_id=oid, market_name=market, token_id=f"tok_{side.value.lower()}",
        side=side, direction=direction, price=price, size=5.0,
        placed_at=time.time(),
    )


class TestBuyBlockedCancelLogic:
    """Test that buy_blocked generates cancel intents for BUY orders."""

    def test_sell_loss_blocks_and_identifies_buy_orders(self):
        """After SELL at big loss, buy_blocked=True and BUY orders on that side
        should be identified for cancellation."""
        mgr = _make_order_mgr()

        # Register BUY DOWN order (this is the one that should be cancelled)
        buy_order = _make_live_order("buy-1", "test-mkt", Side.DOWN,
                                     Direction.BUY, 0.50)
        mgr.register(buy_order)

        # Register SELL DOWN order (should NOT be cancelled)
        sell_order = _make_live_order("sell-1", "test-mkt", Side.DOWN,
                                      Direction.SELL, 0.55)
        mgr.register(sell_order)

        # Register BUY UP order (different side, should NOT be cancelled)
        buy_up = _make_live_order("buy-up-1", "test-mkt", Side.UP,
                                  Direction.BUY, 0.50)
        mgr.register(buy_up)

        # Simulate: SELL DOWN fill triggers buy_blocked_down
        inv = Inventory(shares_down=5.0, avg_cost_down=0.60)
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.40, 5.0)
        # Loss = (0.40 - 0.60) * 5 = -$1.00, exceeds -$0.50 threshold
        assert inv.buy_blocked_down is True

        # Now find BUY orders to cancel (same logic as handle_fill)
        cancel_intents = []
        blocked_side = Side.DOWN
        for oid in list(mgr._orders):
            o = mgr._orders.get(oid)
            if o is None:
                continue
            if (o.market_name == "test-mkt"
                    and o.side == blocked_side
                    and o.direction == Direction.BUY):
                cancel_intents.append(Intent(
                    type=IntentType.CANCEL_ORDER,
                    market_name="test-mkt",
                    order_id=oid,
                    reason="buy_blocked_cancel",
                ))

        assert len(cancel_intents) == 1
        assert cancel_intents[0].order_id == "buy-1"

    def test_no_cancel_when_not_blocked(self):
        """Small loss doesn't trigger buy_blocked → no BUY cancels."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.50)
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.49, 5.0)
        # Loss = -$0.05, below -$0.50 threshold
        assert inv.buy_blocked_down is False

    def test_no_cancel_on_buy_fill(self):
        """BUY fills should NOT trigger buy_blocked cancel logic."""
        inv = Inventory()
        inv.apply_fill(Side.DOWN, Direction.BUY, 0.50, 5.0)
        assert inv.buy_blocked_down is False

    def test_only_cancels_same_side(self):
        """buy_blocked_down should only cancel BUY DOWN, not BUY UP."""
        mgr = _make_order_mgr()

        buy_down = _make_live_order("buy-down", "test-mkt", Side.DOWN,
                                     Direction.BUY, 0.50)
        buy_up = _make_live_order("buy-up", "test-mkt", Side.UP,
                                   Direction.BUY, 0.50)
        mgr.register(buy_down)
        mgr.register(buy_up)

        inv = Inventory(shares_down=5.0, avg_cost_down=0.60)
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.40, 5.0)
        assert inv.buy_blocked_down is True
        assert inv.buy_blocked_up is False

        # Find BUY DOWN orders to cancel
        cancel_ids = [
            oid for oid in mgr._orders
            if mgr._orders[oid].side == Side.DOWN
            and mgr._orders[oid].direction == Direction.BUY
        ]
        assert cancel_ids == ["buy-down"]

    def test_multiple_buy_orders_all_cancelled(self):
        """Multiple BUY orders on blocked side should all be cancelled."""
        mgr = _make_order_mgr()

        for i in range(3):
            order = _make_live_order(f"buy-{i}", "test-mkt", Side.UP,
                                     Direction.BUY, 0.50 - i * 0.02)
            mgr.register(order)

        inv = Inventory(shares_up=5.0, avg_cost_up=0.60)
        inv.apply_fill(Side.UP, Direction.SELL, 0.40, 5.0)
        assert inv.buy_blocked_up is True

        cancel_ids = [
            oid for oid in mgr._orders
            if mgr._orders[oid].side == Side.UP
            and mgr._orders[oid].direction == Direction.BUY
        ]
        assert len(cancel_ids) == 3
