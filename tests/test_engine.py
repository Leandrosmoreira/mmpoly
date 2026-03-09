"""Unit tests for core/engine.py — state machine + grid intent generation."""

import time
import pytest
from unittest.mock import MagicMock

from core.types import (
    BotConfig, BotState, Direction, GridConfig, Intent, IntentType,
    Inventory, LiveOrder, MarketState, Quote, Side, SomaConfig, TimeRegime, TopOfBook,
)
from core.engine import Engine


def make_order_mgr(orders: dict[str, LiveOrder] | None = None):
    """Create a mock OrderManager with optional pre-registered orders."""
    mgr = MagicMock()
    _orders = orders or {}
    mgr.get = lambda oid: _orders.get(oid)
    mgr.get_order_ids_for_market = MagicMock(return_value=[])
    return mgr


class TestIdleToQuoting:
    """Transitions IDLE -> QUOTING when books valid and time ok."""

    def test_idle_transitions_to_quoting(self, cfg, market_state):
        market_state.state = BotState.IDLE
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        intents = engine.tick([], order_mgr)
        assert market_state.state == BotState.QUOTING
        # IDLE tick returns empty (no orders placed until next tick)
        assert len(intents) == 0

    def test_idle_stays_idle_invalid_book(self, cfg, market_state):
        market_state.state = BotState.IDLE
        market_state.book_up = TopOfBook()  # invalid book (zeros)
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        engine.tick([], order_mgr)
        assert market_state.state == BotState.IDLE


class TestExitTriggersExiting:
    """Time < t_exit -> transitions to EXITING and cancels all."""

    def test_exit_regime_triggers_exiting(self, cfg, market_state):
        market_state.end_ts = time.time() + 5.0  # 5s left < t_exit=15
        market_state.state = BotState.QUOTING
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        order1 = LiveOrder(
            order_id="o1", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY, price=0.51, size=5.0,
            placed_at=time.time(), level=0,
        )
        intents = engine.tick(["o1"], order_mgr)

        assert market_state.state == BotState.EXITING
        cancel_intents = [i for i in intents if i.type == IntentType.CANCEL_ORDER]
        assert len(cancel_intents) >= 1  # cancels the live order


class TestHardLimitCancels:
    """net > hard_limit -> cancel all orders."""

    def test_hard_limit_cancels_all(self, cfg, market_state):
        market_state.inventory = Inventory(shares_up=30.0)  # net=30 > 25
        market_state.state = BotState.QUOTING
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        intents = engine.tick(["o1", "o2"], order_mgr)
        cancels = [i for i in intents if i.type == IntentType.CANCEL_ORDER]
        assert len(cancels) == 2  # both orders cancelled


class TestRebalancingEntryExit:
    """Entry/exit to REBALANCING state based on net_soft_limit."""

    def test_enters_rebalancing(self, cfg, market_state):
        market_state.inventory = Inventory(shares_up=12.0)  # net=12 > 10
        market_state.state = BotState.QUOTING
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        engine.tick([], order_mgr)
        assert market_state.state == BotState.REBALANCING

    def test_exits_rebalancing(self, cfg, market_state):
        market_state.inventory = Inventory(shares_up=3.0)  # net=3 < 10*0.5=5
        market_state.state = BotState.REBALANCING
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        engine.tick([], order_mgr)
        assert market_state.state == BotState.QUOTING


class TestSelectiveCancel:
    """Only cancels levels where price changed >= 1 tick."""

    def test_cancel_on_price_change(self, cfg, market_state):
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        # Existing order at level 0 BUY UP at 0.51
        existing = LiveOrder(
            order_id="o1", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY, price=0.51, size=5.0,
            placed_at=time.time(), level=0,
        )
        order_mgr.get = lambda oid: existing if oid == "o1" else None

        # New quote at level 0 BUY UP at 0.53 (moved 2 ticks)
        new_quotes = [Quote(
            side=Side.UP, direction=Direction.BUY,
            price=0.53, size=5.0, level=0,
        )]

        result = engine._selective_cancel(["o1"], new_quotes, order_mgr)
        assert "o1" in result  # price moved >= threshold

    def test_no_cancel_same_price(self, cfg, market_state):
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        existing = LiveOrder(
            order_id="o1", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY, price=0.51, size=5.0,
            placed_at=time.time(), level=0,
        )
        order_mgr.get = lambda oid: existing if oid == "o1" else None

        new_quotes = [Quote(
            side=Side.UP, direction=Direction.BUY,
            price=0.51, size=5.0, level=0,
        )]

        result = engine._selective_cancel(["o1"], new_quotes, order_mgr)
        assert "o1" not in result  # price same, no cancel

    def test_cancel_removed_level(self, cfg, market_state):
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        existing = LiveOrder(
            order_id="o1", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY, price=0.51, size=5.0,
            placed_at=time.time(), level=0,
        )
        order_mgr.get = lambda oid: existing if oid == "o1" else None

        # Level not in new_quotes at all
        result = engine._selective_cancel(["o1"], [], order_mgr)
        assert "o1" in result  # level removed, must cancel


class TestStaleBookGeneratesSell:
    """Sell intents generated when book is stale but has inventory."""

    def test_stale_book_sell(self, cfg, market_state):
        market_state.inventory = Inventory(shares_up=5.0, avg_cost_up=0.50)
        market_state.book_up.ts = time.time() - 10.0  # 10s old, stale_book_ms=5000
        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()
        # Force past throttle
        engine._last_quote_ts = 0

        intents = engine.tick([], order_mgr)
        sells = [
            i for i in intents
            if i.type == IntentType.PLACE_ORDER
            and i.direction == Direction.SELL
        ]
        assert len(sells) >= 1
        assert sells[0].reason.startswith("stale_book_reduce")


class TestExitUsesHasBid:
    """Exit sells work with book that has bid but isn't fully valid."""

    def test_exit_sells_with_has_bid(self, cfg, market_state):
        market_state.state = BotState.EXITING
        market_state.regime = TimeRegime.EXIT
        market_state.end_ts = time.time() + 5.0
        market_state.inventory = Inventory(shares_up=5.0, avg_cost_up=0.50)
        # Book with bid but invalid (no bid_sz)
        market_state.book_up = TopOfBook(
            token_id="tok_up", best_bid=0.48, best_bid_sz=0.0,
            best_ask=0.55, best_ask_sz=0.0, ts=time.time(),
        )
        assert not market_state.book_up.is_valid
        assert market_state.book_up.has_bid

        engine = Engine(market_state, cfg)
        order_mgr = make_order_mgr()

        intents = engine.tick([], order_mgr)
        sells = [
            i for i in intents
            if i.type == IntentType.PLACE_ORDER
            and i.direction == Direction.SELL
            and i.side == Side.UP
        ]
        assert len(sells) >= 1
        assert sells[0].price == 0.48  # best_bid (taker in exit)
