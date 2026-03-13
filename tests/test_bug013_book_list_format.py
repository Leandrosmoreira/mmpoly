"""BUG-013 regression: BookCache crash on list-format book data.

Polymarket WS may send bid/ask levels as arrays [price, size]
instead of dicts {"price": str, "size": str}.
Calling .get() on a list raises: 'list' object has no attribute 'get'.

These tests verify _normalize_level and BookCache handle both formats.
"""

import pytest
from data.book import BookCache, _normalize_level


class TestNormalizeLevel:
    """Test the level normalizer handles all WS formats."""

    def test_dict_format_passthrough(self):
        item = {"price": "0.55", "size": "100"}
        result = _normalize_level(item)
        assert result == item

    def test_list_string_format(self):
        """Array format: ["0.55", "100"]"""
        result = _normalize_level(["0.55", "100"])
        assert result == {"price": "0.55", "size": "100"}

    def test_list_numeric_format(self):
        """Numeric array: [0.55, 100]"""
        result = _normalize_level([0.55, 100])
        assert result == {"price": "0.55", "size": "100"}

    def test_tuple_format(self):
        result = _normalize_level(("0.42", "50"))
        assert result == {"price": "0.42", "size": "50"}

    def test_none_returns_none(self):
        assert _normalize_level(None) is None

    def test_string_returns_none(self):
        assert _normalize_level("0.55") is None

    def test_short_list_returns_none(self):
        assert _normalize_level(["0.55"]) is None

    def test_empty_list_returns_none(self):
        assert _normalize_level([]) is None

    def test_integer_returns_none(self):
        assert _normalize_level(42) is None


class TestBookCacheListFormat:
    """BookCache.update() with list-format levels (the crash case)."""

    def test_update_with_list_bids(self):
        """WS sends bids as [["0.50", "10"], ["0.49", "20"]]."""
        cache = BookCache()
        cache.update("token1",
                     bids=[["0.50", "10"], ["0.49", "20"]],
                     asks=[["0.55", "5"]])
        book = cache.get("token1")
        assert book is not None
        assert book.best_bid == 0.50
        assert book.best_bid_sz == 10.0
        assert book.best_ask == 0.55

    def test_update_with_dict_bids(self):
        """Standard dict format still works."""
        cache = BookCache()
        cache.update("token2",
                     bids=[{"price": "0.50", "size": "10"}],
                     asks=[{"price": "0.55", "size": "5"}])
        book = cache.get("token2")
        assert book is not None
        assert book.best_bid == 0.50
        assert book.best_ask == 0.55

    def test_update_with_mixed_formats(self):
        """Mix of dict and list levels in same message."""
        cache = BookCache()
        cache.update("token3",
                     bids=[{"price": "0.50", "size": "10"}, ["0.49", "20"]],
                     asks=[["0.55", "5"]])
        book = cache.get("token3")
        assert book is not None
        assert book.best_bid == 0.50  # highest bid
        assert book.best_ask == 0.55

    def test_update_with_zero_size_removal(self):
        """Zero-size levels should be filtered out."""
        cache = BookCache()
        cache.update("token4",
                     bids=[["0.50", "10"], ["0.48", "0"]],
                     asks=[["0.55", "5"]])
        book = cache.get("token4")
        assert book.best_bid == 0.50

    def test_update_from_snapshot_list_format(self):
        """REST snapshot with array-format levels."""
        cache = BookCache()
        cache.update_from_snapshot("token5", {
            "bids": [["0.45", "100"], ["0.44", "50"]],
            "asks": [["0.55", "30"]],
        })
        book = cache.get("token5")
        assert book is not None
        assert book.best_bid == 0.45
        assert book.best_ask == 0.55

    def test_no_crash_on_garbage_data(self):
        """Should not raise on completely invalid data."""
        cache = BookCache()
        # Should handle gracefully, not crash
        cache.update("token6",
                     bids=[None, "garbage", 42, []],
                     asks=[{"price": "0.55", "size": "5"}])
        book = cache.get("token6")
        assert book is not None
        assert book.best_ask == 0.55
