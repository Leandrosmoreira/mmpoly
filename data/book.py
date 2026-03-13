"""Order book cache — maintains top-of-book per token.

Handles both full snapshots (REST) and incremental updates (WS).
"""

from __future__ import annotations

import time
import structlog
from typing import Optional

from core.types import TopOfBook

log = structlog.get_logger()


def _normalize_level(item) -> Optional[dict]:
    """Normalize a bid/ask level to {"price": str, "size": str}.

    BUG-013: Polymarket WS may send levels as:
    - dict: {"price": "0.55", "size": "100"} → standard
    - list: ["0.55", "100"] → array format (price, size)
    - list: [0.55, 100] → numeric array
    Returns None if item is unparseable.
    """
    if isinstance(item, dict):
        return item
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return {"price": str(item[0]), "size": str(item[1])}
    return None


class BookCache:
    """Order book cache. Single-thread async safe."""

    def __init__(self):
        self._books: dict[str, TopOfBook] = {}

    def update(self, token_id: str, bids: list, asks: list):
        """Update book from WS message.

        Polymarket WS can send full or partial updates.
        bids/asks format: [{"price": "0.55", "size": "100"}, ...]
        or array format: [["0.55", "100"], ...]
        A size of "0" means remove that level.
        """
        book = self._books.get(token_id)
        if book is None:
            book = TopOfBook(token_id=token_id)
            self._books[token_id] = book

        # BUG-013: normalize levels to dict format before filtering
        norm_bids = [_normalize_level(b) for b in bids]
        norm_asks = [_normalize_level(a) for a in asks]
        norm_bids = [b for b in norm_bids if b is not None]
        norm_asks = [a for a in norm_asks if a is not None]

        # Filter out zero-size levels (removals)
        valid_bids = [b for b in norm_bids if float(b.get("size", 0)) > 0]
        valid_asks = [a for a in norm_asks if float(a.get("size", 0)) > 0]

        if valid_bids:
            # Sort descending by price, take best
            sorted_bids = sorted(valid_bids, key=lambda x: float(x["price"]), reverse=True)
            book.best_bid = float(sorted_bids[0]["price"])
            book.best_bid_sz = float(sorted_bids[0]["size"])
        elif bids:
            # All bids were removals — keep old bid if we have one
            pass
        # If no bids at all in message, keep existing

        if valid_asks:
            sorted_asks = sorted(valid_asks, key=lambda x: float(x["price"]))
            book.best_ask = float(sorted_asks[0]["price"])
            book.best_ask_sz = float(sorted_asks[0]["size"])
        elif asks:
            pass

        book.ts = time.time()

    def update_from_snapshot(self, token_id: str, data):
        """Update from REST snapshot (full book replacement).

        Aceita dict {"bids": [...], "asks": [...]}
        ou objeto OrderBookSummary do py_clob_client (com .bids/.asks).
        """
        # Normaliza para listas de dicts {"price": str, "size": str}
        if isinstance(data, dict):
            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])
            # BUG-013: normalize in case REST also sends array format
            bids = [_normalize_level(b) for b in raw_bids]
            asks = [_normalize_level(a) for a in raw_asks]
            bids = [b for b in bids if b is not None]
            asks = [a for a in asks if a is not None]
        else:
            # OrderBookSummary: atributos .bids/.asks com objetos .price/.size
            raw_bids = getattr(data, "bids", []) or []
            raw_asks = getattr(data, "asks", []) or []
            bids = [{"price": str(b.price), "size": str(b.size)} for b in raw_bids]
            asks = [{"price": str(a.price), "size": str(a.size)} for a in raw_asks]

        book = TopOfBook(token_id=token_id)

        valid_bids = [b for b in bids if float(b.get("size", 0)) > 0]
        valid_asks = [a for a in asks if float(a.get("size", 0)) > 0]

        if valid_bids:
            sorted_bids = sorted(valid_bids, key=lambda x: float(x["price"]), reverse=True)
            book.best_bid = float(sorted_bids[0]["price"])
            book.best_bid_sz = float(sorted_bids[0]["size"])

        if valid_asks:
            sorted_asks = sorted(valid_asks, key=lambda x: float(x["price"]))
            book.best_ask = float(sorted_asks[0]["price"])
            book.best_ask_sz = float(sorted_asks[0]["size"])

        book.ts = time.time()
        self._books[token_id] = book

    def get(self, token_id: str) -> Optional[TopOfBook]:
        return self._books.get(token_id)

    def is_stale(self, token_id: str, max_age_ms: float) -> bool:
        book = self._books.get(token_id)
        if book is None:
            return True
        return book.is_stale(max_age_ms)
