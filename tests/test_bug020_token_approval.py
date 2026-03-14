"""BUG-020: Token approval for SELL orders.

Polymarket requires token approval (allowance) before SELL orders can
be placed. Without approval, SELL orders fail with:
  "not enough balance / allowance"

The fix:
  1. PolyClient.approve_token() calls update_balance_allowance() per token
  2. Tokens are approved at market registration (startup + scanner)
  3. On allowance error during place_order(), auto-approve and retry once
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.types import BotConfig, Direction, Intent, IntentType, Side
from core.errors import ErrorCode
from execution.poly_client import PolyClient


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestApproveToken:
    """Test PolyClient.approve_token()."""

    def test_dry_run_approve(self):
        """Dry run mode should mark tokens as approved."""
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        assert run(client.approve_token("token_abc")) is True
        assert "token_abc" in client._approved_tokens

    def test_approve_idempotent(self):
        """Approving same token twice should be a no-op."""
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        assert run(client.approve_token("token_abc")) is True
        assert run(client.approve_token("token_abc")) is True
        assert len(client._approved_tokens) == 1

    def test_approve_no_client(self):
        """Without a CLOB client, approve should fail gracefully."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        client._client = None
        assert run(client.approve_token("token_abc")) is False

    def test_approve_calls_update_balance_allowance(self):
        """Real approve should call update_balance_allowance."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        mock_clob.update_balance_allowance.return_value = {"success": True}
        client._client = mock_clob

        assert run(client.approve_token("token_xyz")) is True
        assert "token_xyz" in client._approved_tokens
        mock_clob.update_balance_allowance.assert_called_once()

        # Check params
        call_args = mock_clob.update_balance_allowance.call_args
        params = call_args[0][0]
        assert params.token_id == "token_xyz"

    def test_approve_exception_returns_false(self):
        """If approval raises, return False and don't cache."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        mock_clob.update_balance_allowance.side_effect = Exception("network error")
        client._client = mock_clob

        assert run(client.approve_token("token_fail")) is False
        assert "token_fail" not in client._approved_tokens

    def test_approve_multiple_tokens(self):
        """Can approve multiple different tokens."""
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        run(client.approve_token("token_up"))
        run(client.approve_token("token_down"))
        assert len(client._approved_tokens) == 2


class TestPlaceOrderAutoApprove:
    """Test auto-approve retry on allowance error during place_order."""

    def _make_sell_intent(self):
        return Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.82,
            size=5.0,
        )

    def test_allowance_error_triggers_auto_approve(self):
        """First SELL fails with allowance → approve → retry succeeds."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()

        # First call raises allowance error, second succeeds
        call_count = [0]
        def mock_create_order(args):
            return {"signed": True}

        def mock_post_order(signed, order_type):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance'}]")
            return {"success": True, "orderID": "order_123"}

        mock_clob.create_order = mock_create_order
        mock_clob.post_order = mock_post_order
        mock_clob.update_balance_allowance.return_value = {"success": True}
        client._client = mock_clob

        intent = self._make_sell_intent()
        result = run(client.place_order(intent, "token_up_abc"))

        assert result is not None
        assert result.order_id == "order_123"
        assert "token_up_abc" in client._approved_tokens

    def test_retry_even_if_already_approved(self):
        """BUG-024: If token was cached as approved but SELL fails,
        invalidate cache and retry approval (on-chain tx may be stale)."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        client._approved_tokens.add("token_up_abc")  # cached but stale
        mock_clob = MagicMock()

        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.side_effect = Exception(
            "not enough balance / allowance"
        )
        mock_clob.update_balance_allowance.return_value = {"success": True}
        client._client = mock_clob

        intent = self._make_sell_intent()
        result = run(client.place_order(intent, "token_up_abc"))

        # Should have invalidated cache and retried approval
        assert result is None  # retry also fails (post_order still raises)
        mock_clob.update_balance_allowance.assert_called()

    def test_balance_error_buy_no_retry(self):
        """BUY 'not enough balance' → no auto-approve (USDC issue, not token).

        BUG-022: For BUY orders, balance errors = insufficient USDC.
        """
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()

        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.side_effect = Exception(
            "not enough balance"
        )
        client._client = mock_clob

        intent = Intent(type=IntentType.PLACE_ORDER, market_name="btc-15m-test",
                        side=Side.UP, direction=Direction.BUY,
                        price=0.50, size=5.0)
        result = run(client.place_order(intent, "token_up_abc"))

        assert result is None
        assert client._last_place_error == "no_balance"
        mock_clob.update_balance_allowance.assert_not_called()

    def test_balance_error_sell_tries_approve(self):
        """SELL 'not enough balance' → auto-approve + retry.

        BUG-022: For SELL orders, balance errors = token approval needed.
        BUG-024: Always retries, even if token was already in cache.
        """
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()

        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.side_effect = Exception(
            "not enough balance"
        )
        mock_clob.update_balance_allowance.return_value = {"success": True}
        client._client = mock_clob

        intent = self._make_sell_intent()
        result = run(client.place_order(intent, "token_up_abc"))

        assert result is None
        assert client._last_place_error == "allowance"
        # Should have tried approve (but retry will also fail because post_order keeps raising)
        mock_clob.update_balance_allowance.assert_called()

    def test_dry_run_sell_works(self):
        """Dry run mode should work for SELL orders."""
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        intent = self._make_sell_intent()
        result = run(client.place_order(intent, "token_up_abc"))
        assert result is not None
        assert result.direction == Direction.SELL


class TestApprovedTokensState:
    """Test _approved_tokens state management."""

    def test_initial_state_empty(self):
        """New client should have no approved tokens."""
        client = PolyClient(BotConfig())
        assert len(client._approved_tokens) == 0

    def test_approved_persists_across_calls(self):
        """Once approved, token stays approved."""
        cfg = BotConfig(dry_run=True)
        client = PolyClient(cfg)
        run(client.approve_token("t1"))
        run(client.approve_token("t2"))

        # Multiple approve calls don't duplicate
        run(client.approve_token("t1"))
        assert len(client._approved_tokens) == 2
        assert "t1" in client._approved_tokens
        assert "t2" in client._approved_tokens
