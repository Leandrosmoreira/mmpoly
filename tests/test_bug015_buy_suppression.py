"""BUG-015: Buy suppression was too aggressive — killed grid after first fill.

Root cause: `current_pos >= g.level_size` (5 >= 5) stopped ALL buys after
buying just 1 level. The bot became one-shot: buy once → never buy again.

Fix: Only suppress buys when current_pos >= cfg.max_position (respects
grid_levels setting). With grid_levels=1, max_position=10, so the bot
can hold up to 2 fills before stopping.

Also: mid_buy_levels was set to 0 in config, meaning the bot never
bought during MID regime (2-5 min remaining). Changed to 1.
"""

import pytest
from core.quoter import compute_grid_quotes, compute_all_quotes, active_levels
from core.types import (
    BotConfig, Direction, GridConfig, Inventory, Quote,
    Side, SkewResult, TimeRegime, TopOfBook,
)


def _make_book(bid: float = 0.50, ask: float = 0.52, sz: float = 100.0) -> TopOfBook:
    return TopOfBook(
        token_id="tok", best_bid=bid, best_ask=ask,
        best_bid_sz=sz, best_ask_sz=sz, ts=1e9,
    )


def _make_cfg(**overrides) -> BotConfig:
    cfg = BotConfig()
    cfg.grid = GridConfig(
        max_levels=1, level_spacing_ticks=2, level_size=5,
        early_buy_levels=1, early_sell_levels=1,
        mid_buy_levels=1, mid_sell_levels=1,
    )
    cfg.max_position = 10  # grid_levels=1 → level_size * 2
    cfg.net_soft_limit = 5
    cfg.net_hard_limit = 12.5
    cfg.min_spread = 0.01
    cfg.tick = 0.01
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestBuyNotSuppressedAfterFirstFill:
    """The core BUG-015 fix: bot must keep buying after first fill."""

    def test_buy_allowed_with_5_shares_balanced(self):
        """With pos=5 on BOTH sides (net=0), buys should still be allowed.

        Key: net=0 means no inventory skew reduction, so buy_levels stays 1.
        The max_position check (5 < 15) also passes. This is the normal
        market-making case: bought both sides, still quoting.
        """
        book = _make_book(0.50, 0.52)
        inv = Inventory(shares_up=5.0, shares_down=5.0)  # net=0, balanced
        cfg = _make_cfg(max_position=15)
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0, "Should allow buying with net=0, pos=5 < max_pos=15"

    def test_buy_suppressed_at_max_position(self):
        """With pos >= max_position, buys should be suppressed."""
        book = _make_book(0.50, 0.52)
        inv = Inventory(shares_up=10.0, shares_down=0.0)
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0, "Should suppress buys at max_position"

    def test_buy_allowed_zero_pos(self):
        """Sanity: no position → buys allowed."""
        book = _make_book(0.50, 0.52)
        inv = Inventory()
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0

    def test_sell_still_works_with_pos(self):
        """With pos=5, should generate SELL quotes."""
        book = _make_book(0.50, 0.52)
        inv = Inventory(shares_up=5.0, shares_down=0.0)
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        sell_quotes = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sell_quotes) > 0, "Should generate sells when holding inventory"


class TestMidRegimeBuys:
    """MID regime must allow buying (was mid_buy_levels=0 in old config)."""

    def test_mid_buy_levels_positive(self):
        """active_levels should return buy_levels > 0 in MID with net=0."""
        cfg = _make_cfg()
        inv = Inventory()
        buy_l, sell_l = active_levels(cfg, TimeRegime.MID, inv, Side.UP)
        assert buy_l > 0, "MID regime should have buy_levels > 0"

    def test_mid_generates_buy_quotes(self):
        """MID regime should generate BUY quotes."""
        book = _make_book(0.50, 0.52)
        inv = Inventory()
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.MID, cfg,
        )
        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0


class TestBothSidesQuoted:
    """Bot must quote BOTH UP and DOWN sides simultaneously."""

    def test_both_sides_have_buys(self):
        """compute_all_quotes should produce BUY on both UP and DOWN."""
        book_up = _make_book(0.50, 0.52)
        book_down = _make_book(0.48, 0.50)
        inv = Inventory()
        cfg = _make_cfg()
        quotes = compute_all_quotes(
            book_up, book_down, inv, TimeRegime.EARLY, cfg,
        )
        up_buys = [q for q in quotes if q.side == Side.UP and q.direction == Direction.BUY]
        down_buys = [q for q in quotes if q.side == Side.DOWN and q.direction == Direction.BUY]
        assert len(up_buys) > 0, "Should have UP BUY"
        assert len(down_buys) > 0, "Should have DOWN BUY"

    def test_both_sides_continue_after_balanced_fill(self):
        """After buying 5 on each side (net=0), both sides should still buy."""
        book_up = _make_book(0.50, 0.52)
        book_down = _make_book(0.48, 0.50)
        inv = Inventory(shares_up=5.0, shares_down=5.0)  # balanced, net=0
        cfg = _make_cfg(max_position=15)
        quotes = compute_all_quotes(
            book_up, book_down, inv, TimeRegime.EARLY, cfg,
        )
        up_buys = [q for q in quotes if q.side == Side.UP and q.direction == Direction.BUY]
        down_buys = [q for q in quotes if q.side == Side.DOWN and q.direction == Direction.BUY]
        assert len(up_buys) > 0, "UP buys should work when balanced"
        assert len(down_buys) > 0, "DOWN buys should work when balanced"

    def test_heavy_side_buys_suppressed_by_skew(self):
        """When net=-5, inventory skew suppresses DOWN buys (correct behavior)."""
        book_down = _make_book(0.48, 0.50)
        inv = Inventory(shares_up=0.0, shares_down=5.0)  # net=-5, heavy DOWN
        cfg = _make_cfg(max_position=15, net_soft_limit=5)
        quotes = compute_grid_quotes(
            book_down, Side.DOWN, inv, TimeRegime.EARLY, cfg,
        )
        down_buys = [q for q in quotes if q.direction == Direction.BUY]
        # Inventory skew reduces buy_levels: heavy_buy = 5/5 = 1 → buy_l = max(0, 1-1) = 0
        assert len(down_buys) == 0, "Heavy side should have buys suppressed by inv skew"


class TestSpreadCapture:
    """Market-making profit: buy low, sell high (at least 1 tick spread)."""

    def test_buy_below_sell(self):
        """BUY price should be at least 1 tick below SELL price."""
        book = _make_book(0.50, 0.52)
        inv = Inventory(shares_up=5.0)  # has inventory to sell
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        sells = [q for q in quotes if q.direction == Direction.SELL]
        if buys and sells:
            best_buy = max(q.price for q in buys)
            best_sell = min(q.price for q in sells)
            assert best_sell - best_buy >= cfg.tick, (
                f"Need at least 1 tick spread: buy={best_buy} sell={best_sell}"
            )

    def test_no_cross_ask_with_tight_spread(self):
        """BUY price must not cross the ask (POST_ONLY)."""
        book = _make_book(0.50, 0.51)  # 1 tick spread
        inv = Inventory()
        cfg = _make_cfg()
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.EARLY, cfg,
        )
        buys = [q for q in quotes if q.direction == Direction.BUY]
        for q in buys:
            assert q.price < book.best_ask, (
                f"BUY at {q.price} would cross ask at {book.best_ask}"
            )
