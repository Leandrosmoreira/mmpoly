"""BUG-028: FOK sell at expiry for ANY remaining inventory.

Previously, the exit logic:
1. Limited sell size to level_size (5 shares), even if holding 24
2. Used GTC order type (no guaranteed fill)
3. Only used FOK for residual < 5 shares (BUG-023)

Fix:
1. Exit sells ALL remaining shares (not limited to level_size)
2. Uses FOK order type for guaranteed fill at expiry
3. Reason="exit_dump_*" triggers FOK in poly_client
"""

import pytest
import time
from unittest.mock import MagicMock

from core.types import (
    BotConfig, BotState, Direction, GridConfig, Intent, IntentType,
    Inventory, MarketState, Side, TimeRegime, TopOfBook,
)
from core.engine import Engine
from execution.order_manager import OrderManager


def _make_engine(inv: Inventory = None, t_remain: float = 10.0) -> Engine:
    cfg = BotConfig(t_exit=15.0)
    market = MarketState(
        name="btc-15m-test",
        condition_id="cond_1",
        token_up="token_up",
        token_down="token_down",
        inventory=inv or Inventory(),
        book_up=TopOfBook(best_bid=0.90, best_ask=0.92, best_bid_sz=100, best_ask_sz=100, ts=time.time()),
        book_down=TopOfBook(best_bid=0.05, best_ask=0.08, best_bid_sz=100, best_ask_sz=100, ts=time.time()),
        state=BotState.EXITING,
        regime=TimeRegime.EXIT,
        end_ts=time.time() + t_remain,
    )
    return Engine(market, cfg)


class TestExitDump:
    """BUG-028: Exit sells all inventory via FOK."""

    def test_exit_sells_all_shares(self):
        """Exit should sell ALL shares, not just level_size."""
        inv = Inventory(shares_down=24.0, avg_cost_down=0.22)
        engine = _make_engine(inv)
        order_mgr = OrderManager(BotConfig())

        intents = engine._exit_intents([], order_mgr)

        sell_intents = [i for i in intents if i.direction == Direction.SELL]
        assert len(sell_intents) == 1
        assert sell_intents[0].size == 24.0  # ALL shares, not 5

    def test_exit_reason_is_dump(self):
        """Exit intents should have reason starting with 'exit_dump_'."""
        inv = Inventory(shares_up=10.0, shares_down=5.0)
        engine = _make_engine(inv)
        order_mgr = OrderManager(BotConfig())

        intents = engine._exit_intents([], order_mgr)

        for intent in intents:
            assert intent.reason.startswith("exit_dump_")

    def test_exit_both_sides(self):
        """When holding both sides, sells both."""
        inv = Inventory(shares_up=10.0, shares_down=5.0)
        engine = _make_engine(inv)
        order_mgr = OrderManager(BotConfig())

        intents = engine._exit_intents([], order_mgr)

        sides = {i.side for i in intents}
        assert Side.UP in sides
        assert Side.DOWN in sides

    def test_exit_no_inventory_no_intents(self):
        """No inventory → no exit intents."""
        inv = Inventory()
        engine = _make_engine(inv)
        order_mgr = OrderManager(BotConfig())

        intents = engine._exit_intents([], order_mgr)
        assert len(intents) == 0


class TestFOKOrderType:
    """Poly client uses FOK for exit_dump orders."""

    def test_exit_dump_triggers_fok(self):
        """Intent with reason='exit_dump_down' should use FOK."""
        from execution.poly_client import PolyClient
        from py_clob_client.clob_types import OrderType

        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"success": True, "orderID": "exit_1"}
        client._client = mock_clob

        import asyncio
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.DOWN,
            direction=Direction.SELL,
            price=0.05,
            size=24.0,
            reason="exit_dump_down",
        )

        result = asyncio.get_event_loop().run_until_complete(
            client.place_order(intent, "token_down_abc")
        )

        assert result is not None
        # Verify FOK was used (second arg to post_order)
        call_args = mock_clob.post_order.call_args
        assert call_args[0][1] == OrderType.FOK
