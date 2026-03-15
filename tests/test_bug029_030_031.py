"""BUG-029, BUG-030, BUG-031: Critical production fixes.

BUG-029: Token approval hangs indefinitely — add 10s timeout.
BUG-030: Book cache allows inverted books (ask < bid) — reject them.
BUG-031: Max position exceeded due to pending BUY orders — count them.
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock

from core.types import (
    BotConfig, BotState, Direction, Inventory, MarketState,
    Side, TimeRegime, TopOfBook,
)
from core.quoter import compute_grid_quotes
from data.book import BookCache


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# === BUG-030: Book cache inverted ===

class TestBookInverted:
    """BUG-030: Reject books where ask <= bid."""

    def test_inverted_book_invalidated(self):
        """When WS update creates ask < bid, invalidate book."""
        cache = BookCache()
        # Initial valid book
        cache.update("token1", [{"price": "0.50", "size": "100"}],
                     [{"price": "0.52", "size": "100"}])
        book = cache.get("token1")
        assert book.is_valid

        # WS update pushes bid above ask (partial update, stale ask)
        cache.update("token1", [{"price": "0.55", "size": "100"}], [])
        book = cache.get("token1")
        # Now bid=0.55, ask=0.52 → inverted → should be invalidated
        assert not book.is_valid
        assert book.best_bid_sz == 0.0  # cleared

    def test_valid_book_passes(self):
        """Normal book (bid < ask) should remain valid."""
        cache = BookCache()
        cache.update("token1", [{"price": "0.50", "size": "100"}],
                     [{"price": "0.52", "size": "100"}])
        book = cache.get("token1")
        assert book.is_valid
        assert book.best_bid == 0.50
        assert book.best_ask == 0.52

    def test_inverted_snapshot_invalidated(self):
        """Snapshot with inverted book should be invalidated."""
        cache = BookCache()
        cache.update_from_snapshot("token1", {
            "bids": [{"price": "0.60", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        })
        book = cache.get("token1")
        assert not book.is_valid

    def test_equal_bid_ask_invalidated(self):
        """When bid == ask, book should be invalidated (no spread)."""
        cache = BookCache()
        cache.update("token1", [{"price": "0.50", "size": "100"}],
                     [{"price": "0.50", "size": "100"}])
        book = cache.get("token1")
        assert not book.is_valid


# === BUG-031: Pending BUY size ===

class TestPendingBuySize:
    """BUG-031: Pending BUY orders count toward max_position."""

    def _make_cfg(self, **overrides) -> BotConfig:
        cfg = BotConfig(min_spread=0.02, max_position=5)
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_pending_buys_block_new_buys(self):
        """With 0 inventory but 5 pending BUY shares, no new BUYs."""
        cfg = self._make_cfg()
        book = TopOfBook(best_bid=0.50, best_ask=0.53,
                         best_bid_sz=100, best_ask_sz=100, ts=time.time())
        inv = Inventory()

        # With 5 pending BUY shares, effective_pos = 0 + 5 = 5 >= max_position
        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.MID, cfg,
            pending_buy_size=5.0,
        )

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) == 0

    def test_no_pending_allows_buys(self):
        """With 0 inventory and 0 pending, BUYs should work."""
        cfg = self._make_cfg()
        book = TopOfBook(best_bid=0.50, best_ask=0.53,
                         best_bid_sz=100, best_ask_sz=100, ts=time.time())
        inv = Inventory()

        quotes = compute_grid_quotes(
            book, Side.UP, inv, TimeRegime.MID, cfg,
            pending_buy_size=0.0,
        )

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        assert len(buy_quotes) > 0

    def test_partial_pending_limits_levels(self):
        """With inventory + pending, total must not exceed max_position."""
        cfg = self._make_cfg(max_position=10)
        book = TopOfBook(best_bid=0.50, best_ask=0.53,
                         best_bid_sz=100, best_ask_sz=100, ts=time.time())
        inv = Inventory(shares_down=3.0)

        # 3 inventory + 5 pending = 8, max=10 → room for 1 more level (5 shares would make 13)
        quotes = compute_grid_quotes(
            book, Side.DOWN, inv, TimeRegime.MID, cfg,
            pending_buy_size=5.0,
        )

        buy_quotes = [q for q in quotes if q.direction == Direction.BUY]
        # effective_pos = 8, adding 5 = 13 > 10 → no buys
        assert len(buy_quotes) == 0


# === BUG-029: Token approval timeout ===

class TestApprovalTimeout:
    """BUG-029: Token approval should timeout, not hang forever."""

    def test_approval_has_timeout(self):
        """If update_balance_allowance hangs, approval should fail after timeout."""
        from execution.poly_client import PolyClient

        cfg = BotConfig(dry_run=False)
        client = PolyClient(cfg)
        mock_clob = MagicMock()

        # Simulate a hanging call
        import time as time_mod
        def slow_approve(*args, **kwargs):
            time_mod.sleep(15)  # Would hang for 15s
            return {"success": True}

        mock_clob.update_balance_allowance = slow_approve
        client._client = mock_clob

        # Should timeout at 10s, not hang for 15s
        start = time_mod.time()
        result = _run(client.approve_token("token_slow"))
        elapsed = time_mod.time() - start

        assert result is False  # timed out
        assert elapsed < 12.0  # didn't wait full 15s
        assert "token_slow" not in client._approved_tokens
