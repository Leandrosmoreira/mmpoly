"""Unit tests for execution/order_manager.py — order lifecycle + cancel-on-fill."""

import time
import pytest

from core.types import (
    BotConfig, Direction, Fill, IntentType, LiveOrder, Side,
)
from execution.order_manager import OrderManager


@pytest.fixture
def om(cfg):
    return OrderManager(cfg)


def make_order(
    order_id: str,
    side: Side = Side.UP,
    direction: Direction = Direction.BUY,
    price: float = 0.51,
    level: int = 0,
    ttl_ms: float = 5000.0,
    placed_at: float | None = None,
) -> LiveOrder:
    return LiveOrder(
        order_id=order_id,
        market_name="test-market",
        token_id="tok_up" if side == Side.UP else "tok_down",
        side=side,
        direction=direction,
        price=price,
        size=5.0,
        placed_at=placed_at or time.time(),
        ttl_ms=ttl_ms,
        level=level,
    )


class TestCancelOnFillOpposite:
    """BUY UP fill cancels BUY DOWN (same direction, opposite token)."""

    def test_buy_up_cancels_buy_down(self, om):
        order_up = make_order("o_up", side=Side.UP, direction=Direction.BUY)
        order_down = make_order("o_dn", side=Side.DOWN, direction=Direction.BUY)
        om.register(order_up)
        om.register(order_down)

        fill = Fill(
            order_id="o_up", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY,
            price=0.51, size=5.0, ts=time.time(),
        )
        intents = om.on_fill(fill)

        cancel_ids = [i.order_id for i in intents if i.type == IntentType.CANCEL_ORDER]
        assert "o_dn" in cancel_ids  # opposite token, same direction


class TestCancelOnFillSameDir:
    """BUY UP fill does NOT cancel SELL DOWN."""

    def test_buy_up_does_not_cancel_sell_down(self, om):
        order_up = make_order("o_up", side=Side.UP, direction=Direction.BUY)
        order_sell_down = make_order("o_sd", side=Side.DOWN, direction=Direction.SELL)
        om.register(order_up)
        om.register(order_sell_down)

        fill = Fill(
            order_id="o_up", market_name="test-market", token_id="tok_up",
            side=Side.UP, direction=Direction.BUY,
            price=0.51, size=5.0, ts=time.time(),
        )
        intents = om.on_fill(fill)

        cancel_ids = [i.order_id for i in intents if i.type == IntentType.CANCEL_ORDER]
        assert "o_sd" not in cancel_ids  # different direction, should NOT cancel


class TestExpiredOrders:
    """TTL expired orders are detected."""

    def test_expired_detected(self, om):
        old_order = make_order("o_old", ttl_ms=100.0, placed_at=time.time() - 1.0)
        om.register(old_order)

        intents = om.get_expired_orders()
        assert len(intents) == 1
        assert intents[0].order_id == "o_old"
        assert intents[0].reason == "ttl_expired"

    def test_fresh_not_expired(self, om):
        fresh = make_order("o_fresh", ttl_ms=60000.0)
        om.register(fresh)

        intents = om.get_expired_orders()
        assert len(intents) == 0


class TestGridIndex:
    """Register/remove maintains grid index correctly."""

    def test_register_creates_index(self, om):
        order = make_order("o1", side=Side.UP, direction=Direction.BUY, level=2)
        om.register(order)

        oid = om.get_level_order_id("test-market", Side.UP, Direction.BUY, 2)
        assert oid == "o1"

    def test_remove_clears_index(self, om):
        order = make_order("o1", side=Side.UP, direction=Direction.BUY, level=2)
        om.register(order)
        om.remove("o1")

        oid = om.get_level_order_id("test-market", Side.UP, Direction.BUY, 2)
        assert oid is None

    def test_live_count(self, om):
        om.register(make_order("o1"))
        om.register(make_order("o2", side=Side.DOWN))
        assert om.live_count() == 2
        assert om.live_count("test-market") == 2

        om.remove("o1")
        assert om.live_count() == 1
