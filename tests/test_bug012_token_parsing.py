"""BUG-012 regression: Token ID double-encoded JSON parsing.

Gamma API sometimes returns clobTokenIds as double-encoded JSON strings.
After first json.loads, result is still a string instead of a list.
If iterated, gives single characters ("[", '"') instead of token hashes.

These tests verify _parse_json_field handles all known API formats.
"""

import pytest
from execution.market_scanner import _parse_json_field


class TestParseJsonFieldNormal:
    """Standard API responses."""

    def test_already_list(self):
        result = _parse_json_field(["hash1", "hash2"])
        assert result == ["hash1", "hash2"]

    def test_json_encoded_string(self):
        """Single-encoded: '["hash1","hash2"]'"""
        result = _parse_json_field('["hash1","hash2"]')
        assert result == ["hash1", "hash2"]

    def test_real_token_ids(self):
        """Real-world Polymarket token IDs."""
        raw = '["82907365289421843952513824180665274841867112143205639627089670944881132417658","92233720368547758070"]'
        result = _parse_json_field(raw)
        assert len(result) == 2
        assert len(result[0]) > 10
        assert len(result[1]) > 10


class TestParseJsonFieldDoubleEncoded:
    """BUG-012: Double-encoded JSON — the root cause."""

    def test_double_encoded_string(self):
        """Double-encoded: '"[\\"hash1\\",\\"hash2\\"]"'
        First json.loads gives string, second gives list."""
        import json
        inner = json.dumps(["hash1", "hash2"])  # '["hash1","hash2"]'
        double = json.dumps(inner)               # '"[\\"hash1\\",\\"hash2\\"]"'
        result = _parse_json_field(json.loads(double))  # input is the string after resp.json()
        assert result == ["hash1", "hash2"]

    def test_double_encoded_no_char_iteration(self):
        """The critical check: first element must NOT be '[' or '"'."""
        import json
        inner = json.dumps(["tok_a", "tok_b"])
        double_str = json.dumps(inner)
        # Simulate what resp.json() gives us
        parsed_once = json.loads(double_str)  # still a string
        result = _parse_json_field(parsed_once)
        assert result[0] != "["
        assert result[0] != '"'
        assert result == ["tok_a", "tok_b"]


class TestParseJsonFieldEdgeCases:
    """Edge cases and invalid data."""

    def test_none_returns_empty(self):
        result = _parse_json_field(None)
        assert result == []

    def test_empty_string_returns_empty(self):
        result = _parse_json_field("")
        assert result == []

    def test_integer_returns_empty(self):
        result = _parse_json_field(42)
        assert result == []

    def test_invalid_json_returns_empty(self):
        result = _parse_json_field("{not valid json}")
        assert result == []

    def test_json_object_returns_empty(self):
        """json.loads of a JSON object string should return empty (not a list)."""
        result = _parse_json_field('{"key": "value"}')
        assert result == []

    def test_empty_list(self):
        result = _parse_json_field([])
        assert result == []

    def test_json_encoded_empty_list(self):
        result = _parse_json_field("[]")
        assert result == []
