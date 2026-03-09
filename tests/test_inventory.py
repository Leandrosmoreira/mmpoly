"""Unit tests for core/types.py Inventory + data/inventory.py InventoryTracker."""

import pytest

from core.types import Direction, Fill, Inventory, Side
from data.inventory import InventoryTracker


class TestBuyIncreasesShares:
    """BUY adds shares and tracks avg cost."""

    def test_buy_up(self):
        inv = Inventory()
        inv.apply_fill(Side.UP, Direction.BUY, 0.50, 5.0)
        assert inv.shares_up == 5.0
        assert inv.avg_cost_up == 0.50

    def test_buy_down(self):
        inv = Inventory()
        inv.apply_fill(Side.DOWN, Direction.BUY, 0.45, 5.0)
        assert inv.shares_down == 5.0
        assert inv.avg_cost_down == 0.45


class TestSellRecordsPnl:
    """SELL calculates PnL correctly."""

    def test_sell_profit(self):
        inv = Inventory(shares_up=5.0, avg_cost_up=0.50)
        inv.apply_fill(Side.UP, Direction.SELL, 0.55, 5.0)
        assert inv.shares_up == 0.0
        assert inv.realized_pnl == pytest.approx(0.25)  # (0.55-0.50)*5

    def test_sell_loss(self):
        inv = Inventory(shares_up=5.0, avg_cost_up=0.55)
        inv.apply_fill(Side.UP, Direction.SELL, 0.50, 5.0)
        assert inv.realized_pnl == pytest.approx(-0.25)  # (0.50-0.55)*5


class TestAvgCostWeighted:
    """Avg cost is weighted across multiple buys."""

    def test_weighted_average(self):
        inv = Inventory()
        inv.apply_fill(Side.UP, Direction.BUY, 0.50, 5.0)
        inv.apply_fill(Side.UP, Direction.BUY, 0.60, 5.0)
        assert inv.shares_up == 10.0
        # avg = (0.50*5 + 0.60*5) / 10 = 0.55
        assert inv.avg_cost_up == pytest.approx(0.55)


class TestNetCalculation:
    """net = shares_up - shares_down."""

    def test_net_positive(self):
        inv = Inventory(shares_up=10.0, shares_down=3.0)
        assert inv.net == 7.0

    def test_net_negative(self):
        inv = Inventory(shares_up=3.0, shares_down=10.0)
        assert inv.net == -7.0

    def test_net_zero(self):
        inv = Inventory(shares_up=5.0, shares_down=5.0)
        assert inv.net == 0.0


class TestIdempotentFill:
    """Same order_id only counted once in InventoryTracker."""

    def test_duplicate_fill_skipped(self):
        tracker = InventoryTracker(snapshot_path="test_inv.json")
        fill = Fill(
            order_id="order-1", market_name="test",
            token_id="tok", side=Side.UP, direction=Direction.BUY,
            price=0.50, size=5.0, ts=1000.0,
        )
        tracker.apply_fill(fill)
        tracker.apply_fill(fill)  # duplicate

        inv = tracker.get("test")
        assert inv.shares_up == 5.0  # only counted once

    def test_different_orders_both_counted(self):
        tracker = InventoryTracker(snapshot_path="test_inv.json")
        fill1 = Fill(
            order_id="order-1", market_name="test",
            token_id="tok", side=Side.UP, direction=Direction.BUY,
            price=0.50, size=5.0, ts=1000.0,
        )
        fill2 = Fill(
            order_id="order-2", market_name="test",
            token_id="tok", side=Side.UP, direction=Direction.BUY,
            price=0.52, size=5.0, ts=1001.0,
        )
        tracker.apply_fill(fill1)
        tracker.apply_fill(fill2)

        inv = tracker.get("test")
        assert inv.shares_up == 10.0
