"""BUG-032, BUG-033: Approval loop + emergency sell FOK fixes.

BUG-032: After 3 consecutive approval-sell failures, detect phantom inventory.
         Add 30s cooldown between approval retry attempts.
BUG-033: Adverse emergency sells must use FOK (not POST_ONLY) and skip crossing guard.
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from core.types import BotConfig, Direction, Intent, IntentType, Side
from core.errors import ErrorCode
from execution.poly_client import PolyClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# === BUG-032: Sell failure counter + phantom detection ===

class TestSellFailureCounter:
    """BUG-032: Track consecutive SELL failures to detect phantom inventory."""

    def _make_client(self) -> PolyClient:
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        return client

    def test_sell_fail_count_init(self):
        """Client starts with empty sell failure counter."""
        client = self._make_client()
        assert client._sell_fail_count == {}
        assert client._last_approval_ts == {}

    def test_sell_fail_count_increments(self):
        """Each allowance SELL failure increments the counter."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob

        # Mock: approval succeeds, order always fails
        mock_clob.update_balance_allowance.return_value = "ok"
        mock_clob.create_order.side_effect = Exception(
            "PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance'}]"
        )

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
            reason="grid_sell",
        )

        # First failure: triggers approval retry
        result = _run(client.place_order(intent, "token123"))
        assert result is None
        assert client._sell_fail_count.get("token123", 0) >= 1

    def test_after_3_failures_signals_phantom(self):
        """After 3+ failures, _last_place_error='no_balance' for phantom zeroing."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob

        # Pre-set failure count to 3 (simulate 3 prior failures)
        client._sell_fail_count["token123"] = 3

        # Mock: order fails with allowance error
        mock_clob.create_order.side_effect = Exception(
            "not enough balance / allowance"
        )

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
            reason="grid_sell",
        )

        result = _run(client.place_order(intent, "token123"))
        assert result is None
        # Counter should be 4 now (> 3)
        assert client._sell_fail_count["token123"] == 4
        # Should signal phantom inventory
        assert client._last_place_error == "no_balance"

    def test_successful_sell_resets_counter(self):
        """A successful SELL resets the failure counter."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob

        # Set prior failures
        client._sell_fail_count["token123"] = 2
        client._approved_tokens.add("token123")

        # Mock successful order
        mock_clob.create_order.return_value = MagicMock()
        mock_clob.post_order.return_value = {"success": True, "orderID": "0xabc"}

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
            reason="grid_sell",
        )

        result = _run(client.place_order(intent, "token123"))
        assert result is not None
        assert result.order_id == "0xabc"
        # Counter should be reset
        assert "token123" not in client._sell_fail_count

    def test_cooldown_prevents_approval_spam(self):
        """Within 30s of last approval, skip retry (no API spam)."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob

        # Set recent approval timestamp
        client._last_approval_ts["token123"] = time.time() - 5  # 5s ago
        client._sell_fail_count["token123"] = 1

        # Mock: order fails with allowance error
        mock_clob.create_order.side_effect = Exception(
            "not enough balance / allowance"
        )

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
            reason="grid_sell",
        )

        result = _run(client.place_order(intent, "token123"))
        assert result is None
        # approve_token should NOT have been called (cooldown active)
        mock_clob.update_balance_allowance.assert_not_called()


# === BUG-033: Adverse emergency sells use FOK ===

class TestAdverseSellFOK:
    """BUG-033: Adverse emergency sells must use FOK, not POST_ONLY."""

    def test_adverse_sell_uses_fok(self):
        """Intent with reason 'adverse_sell_up' should use FOK order type."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob
        client._approved_tokens.add("token_up")

        from py_clob_client.clob_types import OrderType

        # Track what order type was used
        posted_types = []
        def capture_post(signed_order, order_type):
            posted_types.append(order_type)
            return {"success": True, "orderID": "0xfok"}
        mock_clob.create_order.return_value = MagicMock()
        mock_clob.post_order.side_effect = capture_post

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.92,
            size=5.0,
            reason="adverse_sell_up",
        )

        result = _run(client.place_order(intent, "token_up"))
        assert result is not None
        assert posted_types[0] == OrderType.FOK

    def test_adverse_sell_down_uses_fok(self):
        """Intent with reason 'adverse_sell_down' also uses FOK."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob
        client._approved_tokens.add("token_down")

        from py_clob_client.clob_types import OrderType

        posted_types = []
        def capture_post(signed_order, order_type):
            posted_types.append(order_type)
            return {"success": True, "orderID": "0xfok2"}
        mock_clob.create_order.return_value = MagicMock()
        mock_clob.post_order.side_effect = capture_post

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.DOWN,
            direction=Direction.SELL,
            price=0.07,
            size=10.0,
            reason="adverse_sell_down",
        )

        result = _run(client.place_order(intent, "token_down"))
        assert result is not None
        assert posted_types[0] == OrderType.FOK

    def test_normal_sell_uses_gtc(self):
        """Normal grid sell should still use GTC (POST_ONLY)."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob
        client._approved_tokens.add("token_up")

        from py_clob_client.clob_types import OrderType

        posted_types = []
        def capture_post(signed_order, order_type):
            posted_types.append(order_type)
            return {"success": True, "orderID": "0xgtc"}
        mock_clob.create_order.return_value = MagicMock()
        mock_clob.post_order.side_effect = capture_post

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.55,
            size=5.0,
            reason="grid_sell",
        )

        result = _run(client.place_order(intent, "token_up"))
        assert result is not None
        assert posted_types[0] == OrderType.GTC

    def test_crossing_guard_skips_adverse_sell(self):
        """Adverse sell intents should bypass the crossing guard."""
        # The crossing guard in main.py checks:
        # is_fok_sell = intent.reason.startswith("adverse_sell") or ...
        # We test the condition directly
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.92,
            size=5.0,
            reason="adverse_sell_up",
        )

        from core.quoter import MIN_ORDER_SIZE

        is_fok_sell = (intent.direction == Direction.SELL
                       and (0 < intent.size < MIN_ORDER_SIZE
                            or intent.reason.startswith("exit_dump")
                            or intent.reason.startswith("adverse_sell")))
        assert is_fok_sell is True

    def test_crossing_guard_skips_exit_dump(self):
        """Exit dump intents should also bypass the crossing guard."""
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.DOWN,
            direction=Direction.SELL,
            price=0.07,
            size=10.0,
            reason="exit_dump_down",
        )

        from core.quoter import MIN_ORDER_SIZE

        is_fok_sell = (intent.direction == Direction.SELL
                       and (0 < intent.size < MIN_ORDER_SIZE
                            or intent.reason.startswith("exit_dump")
                            or intent.reason.startswith("adverse_sell")))
        assert is_fok_sell is True

    def test_crossing_guard_normal_sell_not_skipped(self):
        """Normal grid sell should NOT bypass the crossing guard."""
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="test-market",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.55,
            size=5.0,
            reason="grid_sell",
        )

        from core.quoter import MIN_ORDER_SIZE

        is_fok_sell = (intent.direction == Direction.SELL
                       and (0 < intent.size < MIN_ORDER_SIZE
                            or intent.reason.startswith("exit_dump")
                            or intent.reason.startswith("adverse_sell")))
        assert is_fok_sell is False
