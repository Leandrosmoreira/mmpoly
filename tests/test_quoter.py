"""Unit tests for core/quoter.py — grid quote computation."""

import time
import pytest

from core.types import (
    BotConfig, Direction, GridConfig, Inventory, Quote,
    Side, SomaConfig, TimeRegime, TopOfBook,
)
from core.quoter import (
    active_levels, compute_grid_quotes, compute_all_quotes,
    compute_soma_adjustment, round_price, round_size,
)


class TestBuyGridBasic:
    """Buy quotes placed inside the spread at bid+tick."""

    def test_single_buy_level(self, cfg, book_valid, inv_empty):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_empty, TimeRegime.EARLY, cfg
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buys) == 1  # EARLY = 1 buy level
        assert buys[0].price == round_price(0.50 + 0.01)  # bid + tick
        assert buys[0].size == 5.0

    def test_multi_buy_levels_mid(self, cfg, book_valid, inv_empty):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_empty, TimeRegime.MID, cfg
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        # MID = 5 levels, but levels must fit within spread and max_position
        assert len(buys) >= 1
        # Level 0 should be at bid+tick
        assert buys[0].price == round_price(0.50 + 0.01)
        assert buys[0].level == 0


class TestSellGridBasic:
    """Sell quotes placed inside the spread at ask-tick."""

    def test_sell_with_inventory(self, cfg, book_valid, inv_holding_up):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_holding_up, TimeRegime.EARLY, cfg
        )
        sells = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sells) == 1  # EARLY = 1 sell level
        assert sells[0].price == round_price(0.55 - 0.01)  # ask - tick
        assert sells[0].size == 5.0

    def test_no_sell_without_inventory(self, cfg, book_valid, inv_empty):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_empty, TimeRegime.MID, cfg
        )
        sells = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sells) == 0  # no shares to sell


class TestNoQuotesTightSpread:
    """No quotes when spread < min_spread."""

    def test_tight_spread_returns_empty(self, cfg, inv_empty):
        book = TopOfBook(
            token_id="tok", best_bid=0.50, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        # spread = 0.01, min_spread = 0.02
        quotes = compute_grid_quotes(
            book, Side.UP, inv_empty, TimeRegime.MID, cfg
        )
        assert len(quotes) == 0


class TestPostOnlyConstraint:
    """Buy never crosses ask; sell never crosses bid."""

    def test_buy_does_not_cross_ask(self, cfg, inv_empty):
        book = TopOfBook(
            token_id="tok", best_bid=0.53, best_bid_sz=100,
            best_ask=0.55, best_ask_sz=100, ts=time.time(),
        )
        quotes = compute_grid_quotes(
            book, Side.UP, inv_empty, TimeRegime.MID, cfg
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        for b in buys:
            assert b.price < book.best_ask, \
                f"BUY at {b.price} would cross ask {book.best_ask}"

    def test_sell_does_not_cross_bid(self, cfg):
        inv = Inventory(shares_up=25.0, avg_cost_up=0.45)
        book = TopOfBook(
            token_id="tok", best_bid=0.50, best_bid_sz=100,
            best_ask=0.55, best_ask_sz=100, ts=time.time(),
        )
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.MID, cfg
        )
        sells = [q for q in quotes if q.direction == Direction.SELL]
        for s in sells:
            assert s.price > book.best_bid, \
                f"SELL at {s.price} would cross bid {book.best_bid}"


class TestPriceClamping:
    """All prices must be in [0.01, 0.99]."""

    def test_extreme_low_price(self):
        assert round_price(0.001) == 0.01
        assert round_price(-0.5) == 0.01

    def test_extreme_high_price(self):
        assert round_price(1.5) == 0.99
        assert round_price(0.999) == 0.99

    def test_normal_price(self):
        assert round_price(0.555) == 0.56
        assert round_price(0.501) == 0.50


class TestInventorySkew:
    """Heavy UP reduces buy levels for UP side."""

    def test_heavy_up_reduces_buy_up(self, cfg):
        inv = Inventory(shares_up=10.0, shares_down=0.0)  # net = 10
        buy_l, sell_l = active_levels(cfg, TimeRegime.MID, inv, Side.UP)
        # net=10 / unit=2 = 5 levels removed from buy
        assert buy_l == 0  # all buy levels removed
        assert sell_l == 5  # sell unchanged

    def test_neutral_no_skew(self, cfg, inv_empty):
        buy_l, sell_l = active_levels(cfg, TimeRegime.MID, inv_empty, Side.UP)
        assert buy_l == 5
        assert sell_l == 5


class TestBuySuppressedWhenHolding:
    """buy_levels=0 when current_pos >= level_size."""

    def test_no_buys_when_holding(self, cfg, book_valid, inv_holding_up):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_holding_up, TimeRegime.MID, cfg
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buys) == 0  # holding 5 >= level_size 5


class TestEmergencySellInvalidBook:
    """Emergency sell when book invalid but has bid + inventory."""

    def test_emergency_sell_generated(self, cfg, inv_holding_up):
        book = TopOfBook(
            token_id="tok", best_bid=0.48, best_bid_sz=0.0,
            best_ask=0.55, best_ask_sz=0.0, ts=time.time(),
        )
        # book.is_valid=False (bid_sz=0), but best_bid > 0
        assert not book.is_valid
        quotes = compute_grid_quotes(
            book, Side.UP, inv_holding_up, TimeRegime.MID, cfg
        )
        assert len(quotes) == 1
        assert quotes[0].direction == Direction.SELL
        assert quotes[0].price == round_price(0.48 + 0.01)  # bid + tick

    def test_no_emergency_sell_without_inventory(self, cfg, inv_empty):
        book = TopOfBook(
            token_id="tok", best_bid=0.48, best_bid_sz=0.0,
            best_ask=0.55, best_ask_sz=0.0, ts=time.time(),
        )
        quotes = compute_grid_quotes(
            book, Side.UP, inv_empty, TimeRegime.MID, cfg
        )
        assert len(quotes) == 0


class TestLateRegimeNoBuys:
    """LATE regime: buy_levels=0, only sells."""

    def test_late_no_buy_levels(self, cfg, inv_empty):
        buy_l, sell_l = active_levels(cfg, TimeRegime.LATE, inv_empty, Side.UP)
        assert buy_l == 0
        assert sell_l == 5  # sell grid maintained

    def test_late_only_sells(self, cfg, book_valid, inv_holding_up):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_holding_up, TimeRegime.LATE, cfg
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        sells = [q for q in quotes if q.direction == Direction.SELL]
        assert len(buys) == 0
        assert len(sells) >= 1


class TestExitRegime:
    """EXIT regime: no quotes at all."""

    def test_exit_returns_empty(self, cfg, book_valid, inv_holding_up):
        quotes = compute_grid_quotes(
            book_valid, Side.UP, inv_holding_up, TimeRegime.EXIT, cfg
        )
        assert len(quotes) == 0


class TestRoundSize:
    """Round size enforces Polymarket minimum."""

    def test_below_minimum(self):
        assert round_size(4) == 0
        assert round_size(0) == 0

    def test_at_minimum(self):
        assert round_size(5) == 5

    def test_rounding(self):
        assert round_size(5.4) == 5
        assert round_size(5.6) == 6


class TestSomaAdjustment:
    """Soma check adjusts prices when UP+DOWN mids diverge from 1.0."""

    def test_no_adjustment_when_disabled(self, cfg, book_valid, book_down_valid):
        cfg.soma = SomaConfig(enabled=False)
        up_adj, down_adj = compute_soma_adjustment(book_valid, book_down_valid, cfg)
        assert up_adj == 0.0
        assert down_adj == 0.0

    def test_adjustment_when_overpriced(self, cfg):
        cfg.soma = SomaConfig(
            enabled=True, fair_value=1.0,
            threshold=0.03, max_adjustment=0.03, aggression=0.5,
        )
        book_up = TopOfBook(
            token_id="up", best_bid=0.55, best_bid_sz=100,
            best_ask=0.60, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.50, best_bid_sz=100,
            best_ask=0.55, best_ask_sz=100, ts=time.time(),
        )
        # mids: UP=0.575, DOWN=0.525, soma=1.10 > 1.0+0.03
        up_adj, down_adj = compute_soma_adjustment(book_up, book_down, cfg)
        assert up_adj > 0  # positive = buys cheaper, sells more expensive
        assert down_adj > 0

    def test_no_adjustment_within_threshold(self, cfg):
        cfg.soma = SomaConfig(
            enabled=True, fair_value=1.0,
            threshold=0.03, max_adjustment=0.03, aggression=0.5,
        )
        book_up = TopOfBook(
            token_id="up", best_bid=0.49, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.49, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        # mids: 0.50 + 0.50 = 1.00 (within threshold)
        up_adj, down_adj = compute_soma_adjustment(book_up, book_down, cfg)
        assert up_adj == 0.0
        assert down_adj == 0.0


class TestCombinedAskFilter:
    """Suppress buys when UP_ask + DOWN_ask >= 1.0 (no edge)."""

    def test_buys_suppressed_when_combined_ask_ge_one(self, cfg, inv_empty):
        """No BUY quotes when UP_ask + DOWN_ask >= fair_value."""
        cfg.soma = SomaConfig(enabled=True, fair_value=1.0)
        book_up = TopOfBook(
            token_id="up", best_bid=0.49, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.49, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        # UP_ask=0.51 + DOWN_ask=0.51 = 1.02 >= 1.0 → suppress buys
        quotes = compute_all_quotes(book_up, book_down, inv_empty,
                                     TimeRegime.EARLY, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0

    def test_buys_allowed_when_combined_ask_lt_one(self, cfg, inv_empty):
        """BUY quotes generated when UP_ask + DOWN_ask < fair_value."""
        cfg.soma = SomaConfig(enabled=True, fair_value=1.0)
        book_up = TopOfBook(
            token_id="up", best_bid=0.44, best_bid_sz=100,
            best_ask=0.48, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.44, best_bid_sz=100,
            best_ask=0.48, best_ask_sz=100, ts=time.time(),
        )
        # UP_ask=0.48 + DOWN_ask=0.48 = 0.96 < 1.0 → buys allowed
        quotes = compute_all_quotes(book_up, book_down, inv_empty,
                                     TimeRegime.EARLY, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0

    def test_sells_still_generated_when_buys_suppressed(self, cfg):
        """SELL quotes still work when buys are suppressed."""
        cfg.soma = SomaConfig(enabled=True, fair_value=1.0)
        inv = Inventory(shares_up=5.0, avg_cost_up=0.50,
                        shares_down=5.0, avg_cost_down=0.50)
        book_up = TopOfBook(
            token_id="up", best_bid=0.49, best_bid_sz=100,
            best_ask=0.53, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.47, best_bid_sz=100,
            best_ask=0.51, best_ask_sz=100, ts=time.time(),
        )
        # UP_ask=0.53 + DOWN_ask=0.51 = 1.04 >= 1.0 → suppress buys
        quotes = compute_all_quotes(book_up, book_down, inv,
                                     TimeRegime.EARLY, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        sell_quotes = [q for q in quotes if q.direction == Direction.SELL]
        assert len(buy_quotes) == 0
        assert len(sell_quotes) > 0

    def test_filter_disabled_when_soma_disabled(self, cfg, inv_empty):
        """Combined ask filter only applies when soma is enabled."""
        cfg.soma = SomaConfig(enabled=False)
        book_up = TopOfBook(
            token_id="up", best_bid=0.49, best_bid_sz=100,
            best_ask=0.53, best_ask_sz=100, ts=time.time(),
        )
        book_down = TopOfBook(
            token_id="dn", best_bid=0.49, best_bid_sz=100,
            best_ask=0.53, best_ask_sz=100, ts=time.time(),
        )
        # UP_ask + DOWN_ask = 1.06 >= 1.0, but soma disabled → buys allowed
        quotes = compute_all_quotes(book_up, book_down, inv_empty,
                                     TimeRegime.EARLY, cfg)
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0
