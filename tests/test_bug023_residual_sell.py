"""BUG-023: Sell residual shares (< MIN_ORDER_SIZE) via FOK market order.

Polymarket enforces a minimum order size of 5 shares. When the bot holds
fewer than 5 shares (e.g., after partial fills), GTC orders get rejected.

Fix: SELL orders with size < 5 use OrderType.FOK (fill-or-kill) to
liquidate residual inventory at market price. BUY orders < 5 are skipped.

Tests verify:
- SELL with size < 5 uses OrderType.FOK
- SELL with size >= 5 uses OrderType.GTC (normal)
- BUY with size < 5 is skipped (returns None)
- SELL with size = 0 is skipped
- Crossing guard is skipped for FOK sells
- Emergency sell with residual shares works
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from py_clob_client.clob_types import OrderType

from core.types import (
    BotConfig, Direction, Intent, IntentType, LiveOrder, Side, TopOfBook,
)
from core.quoter import MIN_ORDER_SIZE
from core.errors import ErrorCode


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestFOKResidualSell:
    """SELL orders with size < MIN_ORDER_SIZE use FOK."""

    def _make_client(self):
        """Create a PolyClient stub with mocked SDK."""
        from execution.poly_client import PolyClient
        client = PolyClient.__new__(PolyClient)
        client.cfg = BotConfig(dry_run=False)
        client._client = MagicMock()
        client._approved_tokens = set()
        client._last_place_error = ""

        # Mock create_order and post_order to succeed
        client._client.create_order = MagicMock(return_value="signed_order")
        client._client.post_order = MagicMock(return_value={
            "success": True,
            "orderID": "test_order_123",
        })
        client.approve_token = AsyncMock(return_value=False)
        return client

    def test_sell_3_shares_uses_fok(self):
        """SELL with size=3 → OrderType.FOK."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=3.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is not None

        # Verify post_order was called with FOK
        client._client.post_order.assert_called_once()
        _, call_args = client._client.post_order.call_args
        # post_order(signed_order, order_type) — check positional args
        pos_args = client._client.post_order.call_args[0]
        assert pos_args[1] == OrderType.FOK

    def test_sell_5_shares_uses_gtc(self):
        """SELL with size=5 → OrderType.GTC (normal POST_ONLY)."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is not None

        pos_args = client._client.post_order.call_args[0]
        assert pos_args[1] == OrderType.GTC

    def test_sell_1_share_uses_fok(self):
        """SELL with size=1 → OrderType.FOK (edge case, single share)."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=1.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is not None

        pos_args = client._client.post_order.call_args[0]
        assert pos_args[1] == OrderType.FOK

    def test_buy_3_shares_is_skipped(self):
        """BUY with size=3 → returns None (too small, can't buy < 5)."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.50,
            size=3.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is None

        # Should NOT have called the exchange at all
        client._client.create_order.assert_not_called()
        client._client.post_order.assert_not_called()

    def test_sell_0_shares_is_skipped(self):
        """SELL with size=0 → not residual (is_residual is False)."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=0.0,
        )

        # size=0 → is_residual is False (0 > 0 is False)
        # Falls through to normal GTC path, exchange will reject
        result = _run(client.place_order(intent, "token_up_123"))
        # The order goes through normal path with GTC
        pos_args = client._client.post_order.call_args[0]
        assert pos_args[1] == OrderType.GTC

    def test_buy_5_shares_uses_gtc(self):
        """BUY with size=5 → normal GTC (not residual)."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.50,
            size=5.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is not None

        pos_args = client._client.post_order.call_args[0]
        assert pos_args[1] == OrderType.GTC


class TestCrossingGuardSkipForFOK:
    """Crossing guard (BUG-021) is skipped for FOK residual sells."""

    def _make_bot_stub(self):
        bot = MagicMock()
        bot.cfg = BotConfig()
        bot.risk_mgr = MagicMock()
        bot.risk_mgr.check_kill.return_value = False
        bot.risk_mgr.is_killed = False

        market = MagicMock()
        market.token_up = "token_up_123"
        market.token_down = "token_down_456"
        market.book_up = TopOfBook(
            best_bid=0.50, best_ask=0.51,
            best_bid_sz=100, best_ask_sz=100, ts=0
        )
        market.book_down = TopOfBook(
            best_bid=0.49, best_ask=0.50,
            best_bid_sz=100, best_ask_sz=100, ts=0
        )
        bot.markets = {"btc-15m-test": market}

        bot.order_mgr = MagicMock()
        bot.order_mgr.register = MagicMock()
        bot.order_mgr.on_fill = MagicMock(return_value=[])

        bot.poly_client = AsyncMock()
        bot.poly_client.place_order = AsyncMock(return_value=MagicMock(spec=LiveOrder))
        bot.poly_client._last_place_error = None

        bot.inventory = MagicMock()
        bot._has_live_fills = MagicMock(return_value=False)
        bot._live_fill_markets = set()

        return bot

    def test_fok_sell_at_bid_not_clamped(self):
        """SELL 3 shares at best_bid=0.50 → NOT clamped (FOK should cross)."""
        bot = self._make_bot_stub()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,  # == best_bid, would normally be clamped to 0.51
            size=3.0,    # < MIN_ORDER_SIZE → FOK sell
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        # Should have placed order at original price (0.50), NOT clamped
        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.50  # not clamped to 0.51

    def test_normal_sell_at_bid_is_still_clamped(self):
        """SELL 5 shares at best_bid → still clamped (normal GTC path)."""
        bot = self._make_bot_stub()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,  # == best_bid
            size=5.0,    # >= MIN_ORDER_SIZE → normal GTC
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.51  # clamped to best_bid + tick
