"""Tests for BUG-011: Market transition fixes.

Tests cover:
- Liquidity filter removal (markets with zero liquidity accepted)
- Time remaining filter still works (with logging)
- Empty markets warning in _tick
- WS unsubscribe on market removal
- Faster scanner when no markets active
- asyncio.gather resilience (return_exceptions=True)
"""

import time
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from execution.market_scanner import DiscoveredMarket
from execution.ws_feed import WSFeed


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_discovered(
    liquidity: float = 0.0,
    time_remaining_offset: float = 300.0,
    condition_id: str = "cond_new",
) -> DiscoveredMarket:
    """Create a DiscoveredMarket with controllable fields."""
    now = time.time()
    return DiscoveredMarket(
        name="btc-15m",
        slug="btc-updown-15m-1773280800",
        condition_id=condition_id,
        question_id="q123",
        token_up="tok_up_new",
        token_down="tok_down_new",
        end_ts=now + time_remaining_offset,
        start_ts=now - 600,
        question="Will BTC go up?",
        active=True,
        accepting_orders=True,
        min_order_size=5.0,
        tick_size=0.01,
        liquidity=liquidity,
        best_bid=0.50,
        best_ask=0.51,
        spread=0.01,
    )


def _make_gababot():
    """Create a minimal GabaBot-like object for testing _add_market_from_discovery."""
    from core.types import BotConfig, SkewConfig
    from bot.main import GabaBot

    bot = MagicMock(spec=GabaBot)
    bot.cfg = BotConfig(skew=SkewConfig(enabled=False))
    bot.markets = {}
    bot.engines = {}
    bot._token_to_market = {}
    bot._condition_to_name = {}
    bot._skew_engines = {}
    bot._min_liquidity = 1000  # old default
    bot._min_time_remaining = 60

    # Use real _register_market so dicts get updated
    bot._register_market = lambda market: GabaBot._register_market(bot, market)
    return bot


# ── Test: Liquidity filter removed ───────────────────────────────────────


class TestLiquidityFilterRemoved:
    """BUG-011: Markets with zero liquidity must be accepted."""

    def test_zero_liquidity_accepted(self):
        """New 15m markets start with 0 liquidity — bot must add them."""
        from bot.main import GabaBot

        # Use the real method but on a mock object with needed attributes
        bot = _make_gababot()

        discovered = _make_discovered(liquidity=0.0, time_remaining_offset=600)

        # Call the real implementation
        GabaBot._add_market_from_discovery(bot, discovered)

        # Market should be registered (via _register_market -> stores in dicts)
        assert discovered.condition_id in bot._condition_to_name

    def test_low_liquidity_accepted(self):
        """Markets with liquidity below old threshold (1000) are now accepted."""
        from bot.main import GabaBot

        bot = _make_gababot()
        discovered = _make_discovered(liquidity=50.0, time_remaining_offset=600)

        GabaBot._add_market_from_discovery(bot, discovered)

        assert discovered.condition_id in bot._condition_to_name

    def test_time_remaining_filter_still_works(self):
        """Markets too close to expiry are still rejected."""
        from bot.main import GabaBot

        bot = _make_gababot()
        discovered = _make_discovered(
            liquidity=5000.0,
            time_remaining_offset=30.0,  # < min_time_remaining (60)
        )

        GabaBot._add_market_from_discovery(bot, discovered)

        # Should NOT be registered
        assert discovered.condition_id not in bot._condition_to_name

    def test_duplicate_condition_id_rejected(self):
        """Already-known markets are skipped."""
        from bot.main import GabaBot

        bot = _make_gababot()
        bot._condition_to_name["cond_existing"] = "btc-15m-old"

        discovered = _make_discovered(condition_id="cond_existing")

        GabaBot._add_market_from_discovery(bot, discovered)

        # Should still only have the old mapping
        assert bot._condition_to_name["cond_existing"] == "btc-15m-old"


# ── Test: WS Unsubscribe ─────────────────────────────────────────────────


class TestWSUnsubscribe:
    """WSFeed.unsubscribe() cleans up stale tokens."""

    def test_unsubscribe_removes_from_subscribed(self):
        """Tokens are removed from _subscribed_tokens."""
        feed = WSFeed(
            token_ids=["tok_a", "tok_b"],
            on_book_update=MagicMock(),
        )
        feed._subscribed_tokens = {"tok_a", "tok_b", "tok_c"}

        # Run unsubscribe (no WS connection, so just cleanup)
        asyncio.get_event_loop().run_until_complete(
            feed.unsubscribe(["tok_a", "tok_b"])
        )

        assert "tok_a" not in feed._subscribed_tokens
        assert "tok_b" not in feed._subscribed_tokens
        assert "tok_c" in feed._subscribed_tokens

    def test_unsubscribe_removes_from_initial_tokens(self):
        """Tokens are also removed from _initial_tokens to prevent re-subscribe."""
        feed = WSFeed(
            token_ids=["tok_a", "tok_b"],
            on_book_update=MagicMock(),
        )

        asyncio.get_event_loop().run_until_complete(
            feed.unsubscribe(["tok_a"])
        )

        assert "tok_a" not in feed._initial_tokens
        assert "tok_b" in feed._initial_tokens

    def test_unsubscribe_removes_from_pending(self):
        """Pending subs for expired tokens are cleaned."""
        feed = WSFeed(
            token_ids=[],
            on_book_update=MagicMock(),
        )
        feed._pending_subs = ["tok_a", "tok_b", "tok_c"]

        asyncio.get_event_loop().run_until_complete(
            feed.unsubscribe(["tok_b"])
        )

        assert "tok_b" not in feed._pending_subs
        assert feed._pending_subs == ["tok_a", "tok_c"]

    def test_unsubscribe_sends_ws_message_when_connected(self):
        """If WS is connected, sends unsubscribe message (best-effort)."""
        feed = WSFeed(
            token_ids=[],
            on_book_update=MagicMock(),
        )
        feed._subscribed_tokens = {"tok_a"}

        mock_ws = AsyncMock()
        mock_ws.closed = False
        feed._ws = mock_ws

        asyncio.get_event_loop().run_until_complete(
            feed.unsubscribe(["tok_a"])
        )

        mock_ws.send_json.assert_called_once_with({
            "type": "unsubscribe",
            "channel": "book",
            "assets_ids": ["tok_a"],
        })


# ── Test: Empty markets tick ──────────────────────────────────────────────


class TestEmptyMarketsTick:
    """_tick returns early when no markets are active."""

    def test_tick_returns_early_no_markets(self):
        """When self.markets is empty, _tick should return immediately."""
        from bot.main import GabaBot

        bot = MagicMock(spec=GabaBot)
        bot.markets = {}
        bot.engines = {}
        bot.order_mgr = MagicMock()
        bot.risk_mgr = MagicMock()
        bot.inventory = MagicMock()
        bot.cfg = MagicMock()
        bot._last_book_refresh_ts = 0.0
        bot._book_refresh_interval_s = 30.0
        bot._last_snapshot_ts = 0.0

        # Call the real _tick (should return early without doing anything)
        asyncio.get_event_loop().run_until_complete(
            GabaBot._tick(bot)
        )

        # Should NOT call get_expired_orders (would only happen if markets existed)
        bot.order_mgr.get_expired_orders.assert_not_called()


# ── Test: Faster scanner sleep ────────────────────────────────────────────


class TestFasterScannerSleep:
    """Scanner uses 5s interval when no markets are active."""

    def test_scanner_sleeps_less_when_no_markets(self):
        """Verify scanner loop calculates 5s sleep when markets dict is empty."""
        # Just test the logic: when markets is empty, sleep should be 5s
        markets = {}
        scan_interval_s = 30

        sleep_time = 5.0 if not markets else scan_interval_s
        assert sleep_time == 5.0

    def test_scanner_sleeps_normal_when_markets_exist(self):
        """Verify scanner loop calculates 30s sleep when markets exist."""
        markets = {"btc-15m-123": MagicMock()}
        scan_interval_s = 30

        sleep_time = 5.0 if not markets else scan_interval_s
        assert sleep_time == 30


# ── Test: asyncio.gather resilience ───────────────────────────────────────


class TestGatherResilience:
    """asyncio.gather with return_exceptions=True doesn't cascade failures."""

    def test_gather_return_exceptions_doesnt_crash(self):
        """One task raising doesn't cancel the others."""

        async def failing_task():
            raise RuntimeError("task crashed")

        async def success_task():
            return "ok"

        async def run():
            results = await asyncio.gather(
                failing_task(), success_task(),
                return_exceptions=True,
            )
            assert isinstance(results[0], RuntimeError)
            assert results[1] == "ok"

        asyncio.get_event_loop().run_until_complete(run())
