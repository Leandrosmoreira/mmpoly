"""Tests for BUG-037: Polymarket minimum notional ($1) check.

When price * size < $1, BUY orders are rejected by Polymarket.
The quoter should skip BUY quotes below $1 notional.
SELL quotes should NOT be blocked (we need to liquidate regardless).
"""

import time

from core.types import (
    BotConfig, Direction, Inventory, Quote, Side, TimeRegime, TopOfBook,
)
from core.quoter import compute_grid_quotes, MIN_ORDER_VALUE


def _make_book(bid, ask):
    return TopOfBook(
        token_id="tok", best_bid=bid, best_ask=ask,
        best_bid_sz=100, best_ask_sz=100, ts=time.time(),
    )


def _make_cfg(**overrides):
    defaults = dict(min_buy_price=0.10, dry_run=True)
    defaults.update(overrides)
    return BotConfig(**defaults)


class TestMinNotionalBuy:
    """BUG-037: BUY orders with notional < $1 should be skipped."""

    def test_buy_skipped_below_min_notional(self):
        """BUY at 0.15 * 5 = $0.75 → skipped."""
        book = _make_book(0.15, 0.17)
        cfg = _make_cfg(min_buy_price=0.10)
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.UP, inv, TimeRegime.MID, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0, "Should skip BUY with notional $0.75 < $1"

    def test_buy_allowed_above_min_notional(self):
        """BUY at 0.50 * 5 = $2.50 → allowed."""
        book = _make_book(0.50, 0.52)
        cfg = _make_cfg()
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.UP, inv, TimeRegime.MID, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0, "Should allow BUY with notional $2.50"

    def test_buy_boundary_at_exactly_one_dollar(self):
        """BUY at 0.20 * 5 = $1.00 → allowed (exactly at boundary)."""
        book = _make_book(0.20, 0.22)
        cfg = _make_cfg()
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.UP, inv, TimeRegime.MID, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0, "Should allow BUY with notional exactly $1.00"


class TestMinNotionalSell:
    """SELL orders should NOT be blocked by min notional."""

    def test_sell_allowed_below_min_notional(self):
        """SELL at low price should still work — need to liquidate."""
        book = _make_book(0.10, 0.12)
        cfg = _make_cfg(min_buy_price=0.05)
        inv = Inventory(shares_up=5.0, avg_cost_up=0.50)

        quotes = compute_grid_quotes(book, Side.UP, inv, TimeRegime.MID, cfg)
        sell_quotes = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sell_quotes) > 0, "SELL should not be blocked by min notional"
