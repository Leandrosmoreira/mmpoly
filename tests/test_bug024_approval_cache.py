"""BUG-024: Approval cache invalidation on SELL failure.

The token approval cache (`_approved_tokens`) could become stale:
the token was added at market registration, but the on-chain tx
hadn't propagated yet. SELL orders would fail repeatedly (95x in
market 1773520200) because the cache prevented re-approval.

Fix: When a SELL fails with "allowance" error, invalidate the cache
entry and always retry approval (once per place_order call).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.types import BotConfig, Direction, Intent, IntentType, Side
from execution.poly_client import PolyClient


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestApprovalCacheInvalidation:
    """BUG-024: Stale approval cache causes repeated SELL failures."""

    def _make_client(self, *, pre_approve: bool = False):
        """Create a PolyClient with a mock CLOB client."""
        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()
        client._client = mock_clob
        if pre_approve:
            client._approved_tokens.add("token_abc")
        return client, mock_clob

    def _sell_intent(self):
        return Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.DOWN,
            direction=Direction.SELL,
            price=0.50,
            size=5.0,
        )

    def test_sell_failure_invalidates_cache(self):
        """SELL fails → cached token removed from _approved_tokens."""
        client, mock = self._make_client(pre_approve=True)

        mock.create_order.return_value = {"signed": True}
        mock.post_order.side_effect = Exception("not enough balance / allowance")
        mock.update_balance_allowance.return_value = {"success": True}

        assert "token_abc" in client._approved_tokens
        _run(client.place_order(self._sell_intent(), "token_abc"))

        # After first failure, cache should have been invalidated then re-added by approve
        # The key point: update_balance_allowance WAS called (cache didn't block it)
        mock.update_balance_allowance.assert_called()

    def test_sell_retries_after_cache_invalidation(self):
        """After invalidating cache, approval + retry happens."""
        client, mock = self._make_client(pre_approve=True)

        call_count = [0]
        def mock_post(signed, order_type):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("not enough balance / allowance")
            return {"success": True, "orderID": "order_999"}

        mock.create_order.return_value = {"signed": True}
        mock.post_order.side_effect = mock_post
        mock.update_balance_allowance.return_value = {"success": True}

        result = _run(client.place_order(self._sell_intent(), "token_abc"))

        assert result is not None
        assert result.order_id == "order_999"
        mock.update_balance_allowance.assert_called()

    def test_no_infinite_recursion(self):
        """If approval succeeds but SELL keeps failing, no infinite loop."""
        client, mock = self._make_client(pre_approve=False)

        mock.create_order.return_value = {"signed": True}
        mock.post_order.side_effect = Exception("not enough balance / allowance")
        mock.update_balance_allowance.return_value = {"success": True}

        # Should NOT raise RecursionError
        result = _run(client.place_order(self._sell_intent(), "token_abc"))
        assert result is None
        # Called approve at most twice (initial + retry), not infinitely
        assert mock.update_balance_allowance.call_count <= 2

    def test_buy_unaffected_by_cache_fix(self):
        """BUY errors still go to no_balance path, no approval retry."""
        client, mock = self._make_client(pre_approve=True)

        mock.create_order.return_value = {"signed": True}
        mock.post_order.side_effect = Exception("not enough balance")

        intent = Intent(
            type=IntentType.PLACE_ORDER,
            market_name="btc-15m-test",
            side=Side.DOWN,
            direction=Direction.BUY,
            price=0.50, size=5.0,
        )
        result = _run(client.place_order(intent, "token_abc"))

        assert result is None
        assert client._last_place_error == "no_balance"
        mock.update_balance_allowance.assert_not_called()
