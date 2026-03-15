"""BUG-022: Distinguish BUY (insufficient USDC) from SELL (token approval).

The Polymarket CLOB returns the same error "not enough balance / allowance"
for both cases. Before this fix, the code always treated it as a token
approval issue (because "allowance" appears in the error string), even
for BUY orders where the real problem is insufficient USDC collateral.

Tests verify:
- BUY failure sets _last_place_error = "no_balance" (not "allowance")
- BUY failure does NOT trigger approve_token
- SELL failure still triggers approve_token + retry
- SELL failure sets _last_place_error = "allowance"
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.types import BotConfig, Direction, Intent, IntentType, Side


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestBuyVsSellErrorHandling:
    """BUY errors = USDC insufficient, SELL errors = token approval."""

    def _make_client(self):
        """Create a PolyClient stub that raises the ambiguous error."""
        from execution.poly_client import PolyClient
        client = PolyClient.__new__(PolyClient)
        client.cfg = BotConfig(dry_run=False)
        client._client = MagicMock()
        client._approved_tokens = set()
        client._last_place_error = ""
        client._sell_fail_count = {}
        client._last_approval_ts = {}

        # Make create_order + post_order raise the ambiguous error
        def raise_balance_error(*args, **kwargs):
            raise Exception(
                "PolyApiException[status_code=400, error_message="
                "{'error': 'not enough balance / allowance'}]"
            )
        client._client.create_order = raise_balance_error
        client.approve_token = AsyncMock(return_value=False)
        return client

    def test_buy_error_sets_no_balance(self):
        """BUY failure → _last_place_error = 'no_balance'."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.50,
            size=5.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is None
        assert client._last_place_error == "no_balance"

    def test_buy_error_does_not_approve(self):
        """BUY failure should NOT call approve_token."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.50,
            size=5.0,
        )

        _run(client.place_order(intent, "token_up_123"))
        client.approve_token.assert_not_called()

    def test_sell_error_sets_allowance(self):
        """SELL failure → _last_place_error = 'allowance'."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
        )

        result = _run(client.place_order(intent, "token_up_123"))
        assert result is None
        assert client._last_place_error == "allowance"

    def test_sell_error_triggers_approve(self):
        """SELL failure should call approve_token."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
        )

        _run(client.place_order(intent, "token_up_123"))
        client.approve_token.assert_called_once_with("token_up_123")

    def test_sell_error_retries_approve_even_if_cached(self):
        """BUG-024: SELL failure with token already approved → invalidate
        cache and retry (on-chain approval may not have propagated)."""
        client = self._make_client()
        client._approved_tokens.add("token_up_123")  # cached but may be stale
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
        )

        _run(client.place_order(intent, "token_up_123"))
        # BUG-024: Should have retried approval (cache invalidated)
        client.approve_token.assert_called_once_with("token_up_123")

    def test_buy_repeated_errors_no_approve_spam(self):
        """Multiple BUY failures should never call approve_token."""
        client = self._make_client()
        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.UP,
            direction=Direction.BUY,
            price=0.50,
            size=5.0,
        )

        for _ in range(5):
            _run(client.place_order(intent, "token_up_123"))

        client.approve_token.assert_not_called()
        assert client._last_place_error == "no_balance"
