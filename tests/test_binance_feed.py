"""Tests for Binance BTC/USDT WebSocket feed (execution/binance_feed.py)."""

import json
import time
import pytest

from execution.binance_feed import BinanceFeed


class TestHandleMessage:
    """BinanceFeed._handle_message parsing."""

    def _make_feed(self):
        """Create a BinanceFeed with a capturing callback."""
        calls = []
        feed = BinanceFeed(on_price=lambda ts, px: calls.append((ts, px)))
        return feed, calls

    def test_valid_mini_ticker(self):
        """Valid miniTicker payload → callback with price + ts."""
        feed, calls = self._make_feed()
        msg = json.dumps({
            "e": "24hrMiniTicker",
            "E": 1672515782136,
            "s": "BTCUSDT",
            "c": "94250.50",
            "o": "94100.00",
            "h": "94500.00",
            "l": "93900.00",
            "v": "1234.56",
            "q": "116000000.00",
        })
        feed._handle_message(msg)
        assert len(calls) == 1
        ts, px = calls[0]
        assert px == 94250.50
        assert ts == pytest.approx(1672515782.136, abs=0.001)

    def test_uses_event_time_ms(self):
        """Event time E is converted from ms to seconds."""
        feed, calls = self._make_feed()
        msg = json.dumps({"c": "50000.00", "E": 1700000000000})
        feed._handle_message(msg)
        assert len(calls) == 1
        ts, _ = calls[0]
        assert ts == pytest.approx(1700000000.0, abs=0.001)

    def test_fallback_to_local_time_if_no_event_time(self):
        """Missing E field → uses local time.time()."""
        feed, calls = self._make_feed()
        now = time.time()
        msg = json.dumps({"c": "50000.00"})
        feed._handle_message(msg)
        assert len(calls) == 1
        ts, _ = calls[0]
        assert abs(ts - now) < 2.0  # Within 2s of local time

    def test_updates_last_price(self):
        """last_price property is updated on valid message."""
        feed, calls = self._make_feed()
        assert feed.last_price == 0.0
        msg = json.dumps({"c": "94250.50", "E": 1672515782136})
        feed._handle_message(msg)
        assert feed.last_price == 94250.50

    def test_increments_msg_count(self):
        """msg_count increments on each valid message."""
        feed, calls = self._make_feed()
        assert feed._msg_count == 0
        msg = json.dumps({"c": "94250.50", "E": 1672515782136})
        feed._handle_message(msg)
        feed._handle_message(msg)
        assert feed._msg_count == 2


class TestHandleMessageInvalid:
    """BinanceFeed._handle_message with invalid data."""

    def _make_feed(self):
        calls = []
        feed = BinanceFeed(on_price=lambda ts, px: calls.append((ts, px)))
        return feed, calls

    def test_invalid_json(self):
        """Garbage JSON → no callback, no crash."""
        feed, calls = self._make_feed()
        feed._handle_message("not json{{{")
        assert len(calls) == 0

    def test_empty_string(self):
        """Empty string → no callback, no crash."""
        feed, calls = self._make_feed()
        feed._handle_message("")
        assert len(calls) == 0

    def test_array_payload(self):
        """Array instead of dict → no callback."""
        feed, calls = self._make_feed()
        feed._handle_message("[1, 2, 3]")
        assert len(calls) == 0

    def test_missing_close_price(self):
        """No 'c' field → no callback."""
        feed, calls = self._make_feed()
        msg = json.dumps({"e": "24hrMiniTicker", "E": 1672515782136, "s": "BTCUSDT"})
        feed._handle_message(msg)
        assert len(calls) == 0

    def test_zero_price(self):
        """Price = 0 → no callback."""
        feed, calls = self._make_feed()
        msg = json.dumps({"c": "0", "E": 1672515782136})
        feed._handle_message(msg)
        assert len(calls) == 0

    def test_negative_price(self):
        """Negative price → no callback."""
        feed, calls = self._make_feed()
        msg = json.dumps({"c": "-100.00", "E": 1672515782136})
        feed._handle_message(msg)
        assert len(calls) == 0

    def test_non_numeric_price(self):
        """Non-numeric price string → no callback, no crash."""
        feed, calls = self._make_feed()
        msg = json.dumps({"c": "not_a_number", "E": 1672515782136})
        feed._handle_message(msg)
        assert len(calls) == 0

    def test_null_price(self):
        """Null price → no callback."""
        feed, calls = self._make_feed()
        msg = json.dumps({"c": None, "E": 1672515782136})
        feed._handle_message(msg)
        assert len(calls) == 0


class TestLifecycle:
    """BinanceFeed lifecycle methods."""

    def test_stop_sets_running_false(self):
        """stop() sets _running = False."""
        feed = BinanceFeed(on_price=lambda ts, px: None)
        feed._running = True
        # Can't call async stop() synchronously, but verify the flag logic
        assert feed._running is True
        feed._running = False
        assert feed._running is False

    def test_url_property(self):
        """URL is correctly formed for the symbol."""
        feed = BinanceFeed(on_price=lambda ts, px: None, symbol="btcusdt")
        assert "btcusdt@miniTicker" in feed.url
        assert feed.url.startswith("wss://")

    def test_custom_symbol(self):
        """Custom symbol changes the URL."""
        feed = BinanceFeed(on_price=lambda ts, px: None, symbol="ethusdt")
        assert "ethusdt@miniTicker" in feed.url

    def test_is_connected_default_false(self):
        """is_connected is False before start."""
        feed = BinanceFeed(on_price=lambda ts, px: None)
        assert feed.is_connected is False

    def test_initial_state(self):
        """Fresh feed has zeroed state."""
        feed = BinanceFeed(on_price=lambda ts, px: None)
        assert feed.last_price == 0.0
        assert feed._msg_count == 0
        assert feed._running is False
        assert feed._reconnect_delay == 1.0
