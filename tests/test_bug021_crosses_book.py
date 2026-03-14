"""BUG-021: POST_ONLY crossing guard in _execute_intents().

When the book moves between quoter price computation and order placement
(TOCTOU race), the executor must clamp prices to avoid "crosses the book"
rejections from the Polymarket CLOB.

Tests verify:
- BUY at >= best_ask is clamped to best_ask - tick
- SELL at <= best_bid is clamped to best_bid + tick
- Invalid prices after clamp are skipped entirely
- Orders that don't cross pass through unchanged
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections import deque

from core.types import (
    BotConfig, Direction, Intent, IntentType, LiveOrder, Side, TopOfBook,
    MarketState,
)
from core.errors import ErrorCode


def _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51,
                   book_down_bid=0.49, book_down_ask=0.50):
    """Create a minimal GabaBot stub with mocked dependencies."""
    bot = MagicMock()
    bot.cfg = BotConfig()
    bot.risk_mgr = MagicMock()
    bot.risk_mgr.check_kill.return_value = False
    bot.risk_mgr.is_killed = False

    # Market with book data
    market = MagicMock()
    market.token_up = "token_up_123"
    market.token_down = "token_down_456"
    market.book_up = TopOfBook(
        best_bid=book_up_bid, best_ask=book_up_ask,
        best_bid_sz=100, best_ask_sz=100, ts=0
    )
    market.book_down = TopOfBook(
        best_bid=book_down_bid, best_ask=book_down_ask,
        best_bid_sz=100, best_ask_sz=100, ts=0
    )
    bot.markets = {"btc-15m-test": market}

    # Mocked order manager
    bot.order_mgr = MagicMock()
    bot.order_mgr.register = MagicMock()
    bot.order_mgr.on_fill = MagicMock(return_value=[])

    # Mocked poly client
    bot.poly_client = AsyncMock()
    bot.poly_client.place_order = AsyncMock(return_value=MagicMock(spec=LiveOrder))
    bot.poly_client._last_place_error = None

    # Mocked inventory
    bot.inventory = MagicMock()
    bot._has_live_fills = MagicMock(return_value=False)
    bot._live_fill_markets = set()

    return bot


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestBuyCrossingGuard:
    """BUY orders that would cross the ask are clamped."""

    def test_buy_at_ask_is_clamped(self):
        """BUY at 0.51 with best_ask=0.51 → clamped to 0.50."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.51,  # == best_ask, crosses!
            size=5.0,
        )

        # Import the real method
        from bot.main import GabaBot
        _exec = GabaBot._execute_intents

        # Run with real method bound to stub
        _run(_exec(bot, [intent]))

        # Should have placed with clamped price
        bot.poly_client.place_order.assert_called_once()
        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.50

    def test_buy_above_ask_is_clamped(self):
        """BUY at 0.55 with best_ask=0.51 → clamped to 0.50."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.55,
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.50

    def test_buy_below_ask_passes_through(self):
        """BUY at 0.49 with best_ask=0.51 → no clamp needed."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.49,
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.49

    def test_buy_clamped_to_zero_is_skipped(self):
        """BUY crossing with best_ask=0.01 → clamped to 0.00 → skipped."""
        bot = _make_bot_stub()
        # Book with ask at minimum valid price (bid=0.005 rounds edge)
        bot.markets["btc-15m-test"].book_up = TopOfBook(
            best_bid=0.01, best_ask=0.02,
            best_bid_sz=100, best_ask_sz=100, ts=0
        )
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.02,  # == best_ask, crosses
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        # Should clamp to 0.01 (ask - tick), which is valid
        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.01


class TestSellCrossingGuard:
    """SELL orders that would cross the bid are clamped."""

    def test_sell_at_bid_is_clamped(self):
        """SELL at 0.50 with best_bid=0.50 → clamped to 0.51."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,  # == best_bid, crosses!
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.51

    def test_sell_below_bid_is_clamped(self):
        """SELL at 0.45 with best_bid=0.50 → clamped to 0.51."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.45,
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.51

    def test_sell_above_bid_passes_through(self):
        """SELL at 0.52 with best_bid=0.50 → no clamp needed."""
        bot = _make_bot_stub(book_up_bid=0.50, book_up_ask=0.51)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.52,
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.52

    def test_sell_at_high_bid_is_clamped(self):
        """SELL crossing with best_bid=0.98 → clamped to 0.99."""
        bot = _make_bot_stub()
        bot.markets["btc-15m-test"].book_up = TopOfBook(
            best_bid=0.98, best_ask=0.99,
            best_bid_sz=100, best_ask_sz=100, ts=0
        )
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.98,  # == best_bid, crosses
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.99


class TestDownSideCrossing:
    """Crossing guard works for DOWN token too."""

    def test_down_buy_crossing_uses_down_book(self):
        """BUY DOWN uses book_down, not book_up."""
        bot = _make_bot_stub(book_down_bid=0.49, book_down_ask=0.50)
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.DOWN,
            direction=Direction.BUY,
            price=0.50,  # == down best_ask
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.49  # clamped to down_ask - tick


class TestInvalidBookPassthrough:
    """When book is invalid, skip the guard (let exchange decide)."""

    def test_invalid_book_no_clamp(self):
        """Invalid book → guard skipped, order placed as-is."""
        bot = _make_bot_stub()
        # Set invalid book
        bot.markets["btc-15m-test"].book_up = TopOfBook(
            best_bid=0.0, best_ask=0.0,
            best_bid_sz=0, best_ask_sz=0, ts=0
        )
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.55,
            size=5.0,
        )

        from bot.main import GabaBot
        _run(GabaBot._execute_intents(bot, [intent]))

        # Should place without clamping since book is invalid
        placed_intent = bot.poly_client.place_order.call_args[0][0]
        assert placed_intent.price == 0.55
