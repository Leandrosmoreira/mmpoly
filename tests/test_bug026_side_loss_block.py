"""BUG-026: Per-side loss tracking — block re-buy after selling at loss.

When the bot sells at a loss on one side (e.g., bought DOWN at 0.56,
sold at 0.27), it should NOT immediately re-buy on that side. This
prevents the "death spiral": buy → sell at loss → buy → sell at loss.

The fix tracks cumulative realized PnL per side. When side_realized
drops below -$0.10, buy_blocked_{side} is set to True, preventing
any new BUY orders on that side for the remainder of the market.
"""

import pytest
from core.types import BotConfig, Direction, Inventory, Side, TimeRegime, TopOfBook
from core.quoter import compute_grid_quotes


def _make_cfg(**overrides) -> BotConfig:
    cfg = BotConfig(min_spread=0.02)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestSideLossTracking:
    """Inventory tracks per-side realized PnL."""

    def test_loss_blocks_rebuy(self):
        """After selling DOWN at a loss, buy_blocked_down = True."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.50)

        # Sell DOWN at 0.30 → loss = (0.30 - 0.50) * 5 = -1.00
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.30, 5.0)

        assert inv.side_realized_down == pytest.approx(-1.00)
        assert inv.buy_blocked_down is True

    def test_profit_does_not_block(self):
        """Selling at a profit should NOT block re-buy."""
        inv = Inventory(shares_up=5.0, avg_cost_up=0.50)

        # Sell UP at 0.55 → profit = (0.55 - 0.50) * 5 = +0.25
        inv.apply_fill(Side.UP, Direction.SELL, 0.55, 5.0)

        assert inv.side_realized_up == pytest.approx(0.25)
        assert inv.buy_blocked_up is False

    def test_small_loss_does_not_block(self):
        """Loss < -$0.10 threshold → no block yet."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.50)

        # Sell DOWN at 0.49 → loss = (0.49 - 0.50) * 5 = -0.05
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.49, 5.0)

        assert inv.side_realized_down == pytest.approx(-0.05)
        assert inv.buy_blocked_down is False

    def test_block_independent_per_side(self):
        """Blocking DOWN does not block UP."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.50,
                        shares_up=5.0, avg_cost_up=0.50)

        # Sell DOWN at loss
        inv.apply_fill(Side.DOWN, Direction.SELL, 0.30, 5.0)

        assert inv.buy_blocked_down is True
        assert inv.buy_blocked_up is False


class TestQuoterRespectsBlock:
    """Quoter suppresses buys when side is blocked."""

    def test_blocked_side_no_buys(self):
        """When buy_blocked_down=True, no DOWN BUY quotes."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.45, best_ask=0.48,
                         best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory(buy_blocked_down=True)

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0

    def test_unblocked_side_has_buys(self):
        """When buy_blocked_down=False, DOWN BUY quotes generated."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.45, best_ask=0.48,
                         best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory(buy_blocked_down=False)

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0

    def test_blocked_side_still_sells(self):
        """Blocked side can still SELL to dump remaining inventory."""
        cfg = _make_cfg()
        book = TopOfBook(best_bid=0.45, best_ask=0.48,
                         best_bid_sz=100, best_ask_sz=100, ts=1.0)
        inv = Inventory(shares_down=5.0, avg_cost_down=0.50,
                        buy_blocked_down=True)

        quotes = compute_grid_quotes(book, Side.DOWN, inv, TimeRegime.MID, cfg)

        sell_quotes = [q for q in quotes if q.direction == Direction.SELL]
        assert len(sell_quotes) > 0
