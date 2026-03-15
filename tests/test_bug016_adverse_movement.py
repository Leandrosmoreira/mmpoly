"""BUG-016: Adverse movement detection — sell at loss before it grows.

Root cause: Bot held losing positions (e.g. bought DOWN at 0.70, held until
it dropped to 0.16) because there was no loss-cutting mechanism. The grid
only places sells at ask-tick, which may never fill when the market moves
against the position.

Fix: Engine detects when unrealized PnL drops below adverse_loss_threshold
and triggers emergency sells at bid price for fast fill.
"""

import time
import pytest
from unittest.mock import MagicMock

from core.engine import Engine
from core.types import (
    BotConfig, BotState, Direction, GridConfig, Intent, IntentType,
    Inventory, MarketState, Side, TimeRegime, TopOfBook,
)


def _make_book(bid: float, ask: float, sz: float = 100.0) -> TopOfBook:
    return TopOfBook(
        token_id="tok", best_bid=bid, best_ask=ask,
        best_bid_sz=sz, best_ask_sz=sz, ts=time.time(),
    )


def _make_cfg(**overrides) -> BotConfig:
    cfg = BotConfig()
    cfg.grid = GridConfig(
        max_levels=1, level_spacing_ticks=2, level_size=5,
        early_buy_levels=1, early_sell_levels=1,
        mid_buy_levels=1, mid_sell_levels=1,
    )
    cfg.max_position = 10
    cfg.net_soft_limit = 5
    cfg.net_hard_limit = 12.5
    cfg.min_spread = 0.01
    cfg.tick = 0.01
    cfg.adverse_loss_threshold = -0.50
    cfg.adverse_sell_at_bid = True
    cfg.t_early = 300
    cfg.t_mid = 120
    cfg.t_late = 30
    cfg.t_exit = 15
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_market(
    inv: Inventory | None = None,
    book_up_bid: float = 0.50,
    book_up_ask: float = 0.52,
    book_down_bid: float = 0.48,
    book_down_ask: float = 0.50,
) -> MarketState:
    market = MarketState(
        name="test-market",
        condition_id="cond123",
        token_up="tok_up",
        token_down="tok_down",
        end_ts=time.time() + 600,
    )
    market.book_up = _make_book(book_up_bid, book_up_ask)
    market.book_down = _make_book(book_down_bid, book_down_ask)
    market.inventory = inv or Inventory()
    market.state = BotState.QUOTING
    return market


def _make_order_mgr():
    mgr = MagicMock()
    mgr.get.return_value = None
    mgr.get_order_ids_for_market.return_value = []
    return mgr


class TestUnrealizedPnl:
    """Test Inventory.unrealized_pnl calculation."""

    def test_no_position(self):
        inv = Inventory()
        assert inv.unrealized_pnl(0.50, 0.50) == 0.0

    def test_profit_up(self):
        inv = Inventory(shares_up=5.0, avg_cost_up=0.50)
        # mid_up=0.60 → profit = (0.60 - 0.50) * 5 = 0.50
        assert abs(inv.unrealized_pnl(0.60, 0.0) - 0.50) < 0.001

    def test_loss_down(self):
        inv = Inventory(shares_down=5.0, avg_cost_down=0.70)
        # mid_down=0.16 → loss = (0.16 - 0.70) * 5 = -2.70
        assert abs(inv.unrealized_pnl(0.0, 0.16) - (-2.70)) < 0.001

    def test_mixed_position(self):
        inv = Inventory(
            shares_up=5.0, avg_cost_up=0.50,
            shares_down=5.0, avg_cost_down=0.50,
        )
        # UP dropped to 0.30, DOWN rose to 0.70
        # UP loss: (0.30 - 0.50) * 5 = -1.00
        # DOWN gain: (0.70 - 0.50) * 5 = +1.00
        assert abs(inv.unrealized_pnl(0.30, 0.70)) < 0.001  # net zero


class TestAdverseMovementDetection:
    """Engine triggers emergency sell when unrealized loss > threshold."""

    def test_triggers_on_loss(self):
        """Large unrealized loss → emergency sell intents."""
        # Bought DOWN at 0.70, now market at 0.16
        inv = Inventory(shares_down=5.0, avg_cost_down=0.70)
        market = _make_market(
            inv=inv,
            book_down_bid=0.15, book_down_ask=0.17,
        )
        cfg = _make_cfg(adverse_loss_threshold=-0.50)
        engine = Engine(market, cfg)
        order_mgr = _make_order_mgr()

        intents = engine.tick([], order_mgr)

        sell_intents = [i for i in intents if i.type == IntentType.PLACE_ORDER
                        and i.direction == Direction.SELL]
        assert len(sell_intents) > 0, "Should trigger emergency sell on adverse move"
        # BUG-034: Price is floored at avg_cost - max_loss_per_share (0.70 - 0.12 = 0.58)
        # Not selling at catastrophic bid of 0.15
        assert sell_intents[0].price == 0.58

    def test_no_trigger_when_profitable(self):
        """No emergency sell when position is profitable."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.40)
        market = _make_market(
            inv=inv,
            book_down_bid=0.48, book_down_ask=0.50,
        )
        cfg = _make_cfg(adverse_loss_threshold=-0.50)
        engine = Engine(market, cfg)
        order_mgr = _make_order_mgr()

        intents = engine.tick([], order_mgr)

        # Should NOT have adverse emergency sells
        adverse_sells = [i for i in intents if i.type == IntentType.PLACE_ORDER
                         and i.reason.startswith("adverse_")]
        assert len(adverse_sells) == 0

    def test_no_trigger_when_empty(self):
        """No trigger with zero inventory."""
        market = _make_market()
        cfg = _make_cfg(adverse_loss_threshold=-0.50)
        engine = Engine(market, cfg)
        order_mgr = _make_order_mgr()

        intents = engine.tick([], order_mgr)
        adverse = [i for i in intents if i.reason.startswith("adverse_")]
        assert len(adverse) == 0

    def test_sell_at_bid_plus_tick_when_disabled(self):
        """When adverse_sell_at_bid=False, sell at bid+tick (POST_ONLY)."""
        inv = Inventory(shares_down=5.0, avg_cost_down=0.70)
        market = _make_market(
            inv=inv,
            book_down_bid=0.15, book_down_ask=0.17,
        )
        cfg = _make_cfg(
            adverse_loss_threshold=-0.50,
            adverse_sell_at_bid=False,
        )
        engine = Engine(market, cfg)
        order_mgr = _make_order_mgr()

        intents = engine.tick([], order_mgr)
        sell_intents = [i for i in intents if i.type == IntentType.PLACE_ORDER
                        and i.direction == Direction.SELL]
        assert len(sell_intents) > 0
        # BUG-034: Price floored at avg_cost - max_loss_per_share (0.70 - 0.12 = 0.58)
        assert sell_intents[0].price == 0.58

    def test_threshold_boundary(self):
        """Exactly at threshold → should NOT trigger (need to exceed)."""
        # loss = (0.60 - 0.70) * 5 = -0.50, threshold = -0.50
        inv = Inventory(shares_down=5.0, avg_cost_down=0.70)
        market = _make_market(
            inv=inv,
            book_down_bid=0.59, book_down_ask=0.61,
        )
        cfg = _make_cfg(adverse_loss_threshold=-0.50)
        engine = Engine(market, cfg)
        order_mgr = _make_order_mgr()

        intents = engine.tick([], order_mgr)
        adverse = [i for i in intents if i.reason.startswith("adverse_")]
        assert len(adverse) == 0, "At exactly threshold, should not trigger"
