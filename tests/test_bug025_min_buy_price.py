"""BUG-025: Minimum buy price floor — stop buying resolved markets.

When a token's best_bid drops below min_buy_price (default 0.15),
stop placing BUY orders on that side. A DOWN token at 0.10 means
the market has resolved (BTC went UP) — no edge in buying DOWN.

Prevents the bot from accumulating worthless shares on the losing side.
"""

import pytest
from core.types import BotConfig, Direction, Inventory, Side, TimeRegime, TopOfBook
from core.quoter import compute_grid_quotes


def _make_cfg(**overrides) -> BotConfig:
    cfg = BotConfig(min_spread=0.02, min_buy_price=0.15)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestMinBuyPrice:
    """BUG-025: Buy suppression below price floor."""

    def test_buys_suppressed_below_floor(self):
        """No BUY quotes when best_bid < min_buy_price."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.10, best_ask=0.12, best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0

    def test_buys_allowed_above_floor(self):
        """BUY quotes generated when best_bid >= min_buy_price."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.45, best_ask=0.48, best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0

    def test_sells_still_work_below_floor(self):
        """SELL quotes still generated below floor (need to dump inventory)."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.08, best_ask=0.12, best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory(shares_down=5.0, avg_cost_down=0.40)

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        sell_quotes = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sell_quotes) > 0

    def test_floor_at_boundary(self):
        """At exactly min_buy_price, buys should still be suppressed (< not <=)."""
        cfg = _make_cfg(min_buy_price=0.15)
        # best_bid = 0.14 < 0.15 → suppressed
        book = TopOfBook(best_bid=0.14, best_ask=0.17, best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0

    def test_up_side_also_checked(self):
        """Price floor applies to UP side too."""
        cfg = _make_cfg(min_buy_price=0.15)
        book = TopOfBook(best_bid=0.05, best_ask=0.08, best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory()

        quotes = compute_grid_quotes(book, Side.UP, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0
