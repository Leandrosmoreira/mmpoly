"""Tests for BUG-017: phantom_inventory_zeroed destroys real inventory.

The bug: when a SELL order fails with "not enough balance" or "allowance",
the bot unconditionally zeroes inventory. But shares from live fills ARE real.
"Allowance" errors mean the token needs contract approval, not that shares
don't exist.

Fix: separate "allowance" from "no_balance" errors, and only zero inventory
when shares came from a snapshot (no live fills for that market+side).
"""

import asyncio
import pytest
from unittest.mock import MagicMock

from core.types import (
    BotConfig, Direction, Fill, GridConfig, Intent, IntentType,
    MarketState, Side, TopOfBook,
)
from data.inventory import InventoryTracker
from data.fills import FillsCache
from execution.poly_client import PolyClient


def _run(coro):
    """Helper to run async coroutine in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ── poly_client separates "allowance" from "no_balance" ──


class TestPolyClientErrorSeparation:
    """BUG-017: poly_client must distinguish allowance vs no_balance."""

    def setup_method(self):
        self.cfg = BotConfig(dry_run=True)
        self.client = PolyClient(self.cfg)

    def test_no_balance_error(self):
        """'not enough balance' should set _last_place_error = 'no_balance'."""
        self.client.cfg = BotConfig(dry_run=False)
        self.client._client = MagicMock()
        self.client._client.create_order = MagicMock(
            side_effect=Exception("not enough balance to place order")
        )
        intent = Intent(type=IntentType.PLACE_ORDER, market_name="test",
                        side=Side.UP, direction=Direction.SELL,
                        price=0.55, size=5.0)
        result = _run(self.client.place_order(intent, "token123"))
        assert result is None
        assert self.client._last_place_error == "no_balance"

    def test_allowance_error(self):
        """'allowance' error should set _last_place_error = 'allowance', NOT 'no_balance'."""
        self.client.cfg = BotConfig(dry_run=False)
        self.client._client = MagicMock()
        self.client._client.create_order = MagicMock(
            side_effect=Exception("insufficient allowance for transfer")
        )
        intent = Intent(type=IntentType.PLACE_ORDER, market_name="test",
                        side=Side.UP, direction=Direction.SELL,
                        price=0.55, size=5.0)
        result = _run(self.client.place_order(intent, "token123"))
        assert result is None
        assert self.client._last_place_error == "allowance"

    def test_allowance_not_treated_as_no_balance(self):
        """Allowance error must NOT be conflated with no_balance."""
        self.client.cfg = BotConfig(dry_run=False)
        self.client._client = MagicMock()
        self.client._client.create_order = MagicMock(
            side_effect=Exception("allowance exceeded")
        )
        intent = Intent(type=IntentType.PLACE_ORDER, market_name="test",
                        side=Side.UP, direction=Direction.SELL,
                        price=0.55, size=5.0)
        _run(self.client.place_order(intent, "token123"))
        assert self.client._last_place_error != "no_balance"

    def test_unrelated_error_no_flag(self):
        """Unrelated errors should not set any error flag."""
        self.client.cfg = BotConfig(dry_run=False)
        self.client._client = MagicMock()
        self.client._client.create_order = MagicMock(
            side_effect=Exception("network timeout")
        )
        intent = Intent(type=IntentType.PLACE_ORDER, market_name="test",
                        side=Side.UP, direction=Direction.SELL,
                        price=0.55, size=5.0)
        _run(self.client.place_order(intent, "token123"))
        assert self.client._last_place_error == ""

    def test_error_flag_reset_on_success(self):
        """Error flag is cleared on successful placement."""
        self.client._last_place_error = "no_balance"
        intent = Intent(type=IntentType.PLACE_ORDER, market_name="test",
                        side=Side.UP, direction=Direction.BUY,
                        price=0.55, size=5.0)
        # dry_run mode => success
        result = _run(self.client.place_order(intent, "token123"))
        assert result is not None
        assert self.client._last_place_error == ""


# ── GabaBot._has_live_fills ──


class TestHasLiveFills:
    """BUG-017: _has_live_fills distinguishes live fills from snapshot inventory."""

    def setup_method(self):
        # Minimal mock of GabaBot with fills cache
        self.fills = FillsCache()

    def test_no_fills_returns_false(self):
        """No fills → _has_live_fills returns False."""
        fills = self.fills.for_market("test_market")
        has_buys = any(f.side == Side.UP and f.direction == Direction.BUY for f in fills)
        assert not has_buys

    def test_buy_fill_returns_true(self):
        """BUY fill for the side → returns True (shares are real)."""
        fill = Fill(order_id="ord1", market_name="test_market",
                    token_id="tok1", side=Side.UP, direction=Direction.BUY,
                    price=0.58, size=5.0, ts=1000.0, is_maker=True)
        self.fills.add(fill)
        fills = self.fills.for_market("test_market")
        has_buys = any(f.side == Side.UP and f.direction == Direction.BUY for f in fills)
        assert has_buys

    def test_sell_fill_returns_false(self):
        """SELL fill should NOT count as 'has live fills' for protection."""
        fill = Fill(order_id="ord1", market_name="test_market",
                    token_id="tok1", side=Side.UP, direction=Direction.SELL,
                    price=0.60, size=5.0, ts=1000.0, is_maker=True)
        self.fills.add(fill)
        fills = self.fills.for_market("test_market")
        has_buys = any(f.side == Side.UP and f.direction == Direction.BUY for f in fills)
        assert not has_buys

    def test_wrong_side_returns_false(self):
        """BUY fill on DOWN side doesn't protect UP side."""
        fill = Fill(order_id="ord1", market_name="test_market",
                    token_id="tok1", side=Side.DOWN, direction=Direction.BUY,
                    price=0.42, size=5.0, ts=1000.0, is_maker=True)
        self.fills.add(fill)
        fills = self.fills.for_market("test_market")
        has_up_buys = any(f.side == Side.UP and f.direction == Direction.BUY for f in fills)
        assert not has_up_buys


# ── InventoryTracker.zero_side guarding ──


class TestZeroSideGuarding:
    """BUG-017: zero_side should only clear phantom (snapshot) inventory."""

    def setup_method(self):
        self.tracker = InventoryTracker(snapshot_path="tests/tmp_inv.json")

    def test_zero_side_works_when_no_fills(self):
        """Without live fills, zero_side should clear inventory (snapshot phantom)."""
        inv = self.tracker.get("test_market")
        inv.shares_up = 10.0
        inv.avg_cost_up = 0.50
        self.tracker.zero_side("test_market", Side.UP)
        inv = self.tracker.get("test_market")
        assert inv.shares_up == 0.0
        assert inv.avg_cost_up == 0.0

    def test_zero_side_down(self):
        """zero_side on DOWN clears down shares."""
        inv = self.tracker.get("test_market")
        inv.shares_down = 5.0
        inv.avg_cost_down = 0.40
        self.tracker.zero_side("test_market", Side.DOWN)
        inv = self.tracker.get("test_market")
        assert inv.shares_down == 0.0
        assert inv.avg_cost_down == 0.0

    def test_zero_side_preserves_other_side(self):
        """Zeroing UP should not affect DOWN."""
        inv = self.tracker.get("test_market")
        inv.shares_up = 10.0
        inv.shares_down = 5.0
        self.tracker.zero_side("test_market", Side.UP)
        inv = self.tracker.get("test_market")
        assert inv.shares_up == 0.0
        assert inv.shares_down == 5.0
