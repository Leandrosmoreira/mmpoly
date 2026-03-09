"""Shared test fixtures for GabaBook MM Bot."""

import time
import pytest

from core.types import (
    BotConfig, BotState, Direction, GridConfig, Inventory,
    MarketState, Side, SomaConfig, TimeRegime, TopOfBook,
)


@pytest.fixture
def cfg() -> BotConfig:
    """Default bot config for tests."""
    return BotConfig(
        tick=0.01,
        min_spread=0.02,
        quote_ttl_ms=5000.0,
        grid=GridConfig(
            max_levels=5,
            level_spacing_ticks=2,
            level_size=5.0,
            early_buy_levels=1,
            early_sell_levels=1,
            mid_buy_levels=5,
            mid_sell_levels=5,
        ),
        soma=SomaConfig(enabled=False),
        net_soft_limit=10.0,
        net_hard_limit=25.0,
        max_position=50.0,
        price_move_threshold=0.01,
        t_early=300.0,
        t_mid=60.0,
        t_late=30.0,
        t_exit=15.0,
        stale_book_ms=5000.0,
    )


@pytest.fixture
def book_valid() -> TopOfBook:
    """Valid order book with a reasonable spread."""
    return TopOfBook(
        token_id="tok_up",
        best_bid=0.50,
        best_bid_sz=100.0,
        best_ask=0.55,
        best_ask_sz=100.0,
        ts=time.time(),
    )


@pytest.fixture
def book_down_valid() -> TopOfBook:
    """Valid DOWN book complementary to book_valid."""
    return TopOfBook(
        token_id="tok_down",
        best_bid=0.45,
        best_bid_sz=100.0,
        best_ask=0.50,
        best_ask_sz=100.0,
        ts=time.time(),
    )


@pytest.fixture
def inv_empty() -> Inventory:
    """Empty inventory."""
    return Inventory()


@pytest.fixture
def inv_holding_up() -> Inventory:
    """Holding 5 UP shares."""
    return Inventory(shares_up=5.0, avg_cost_up=0.50)


@pytest.fixture
def inv_holding_both() -> Inventory:
    """Holding 5 UP + 5 DOWN shares."""
    return Inventory(
        shares_up=5.0, shares_down=5.0,
        avg_cost_up=0.50, avg_cost_down=0.45,
    )


@pytest.fixture
def market_state(book_valid, book_down_valid) -> MarketState:
    """Active market in MID regime with valid books."""
    return MarketState(
        name="test-market",
        condition_id="cond123",
        token_up="tok_up",
        token_down="tok_down",
        book_up=book_valid,
        book_down=book_down_valid,
        state=BotState.QUOTING,
        regime=TimeRegime.MID,
        end_ts=time.time() + 120.0,  # 2 min left => MID regime
        is_active=True,
    )
