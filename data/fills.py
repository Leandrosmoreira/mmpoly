"""Fill history tracking."""

from __future__ import annotations

from collections import deque
from typing import Optional

from core.types import Fill


class FillsCache:
    """Recent fills cache for analysis."""

    def __init__(self, max_size: int = 1000):
        self._fills: deque[Fill] = deque(maxlen=max_size)
        self._by_market: dict[str, deque[Fill]] = {}

    def add(self, fill: Fill):
        self._fills.append(fill)
        if fill.market_name not in self._by_market:
            self._by_market[fill.market_name] = deque(maxlen=200)
        self._by_market[fill.market_name].append(fill)

    def recent(self, n: int = 50) -> list[Fill]:
        return list(self._fills)[-n:]

    def for_market(self, market_name: str, n: int = 50) -> list[Fill]:
        fills = self._by_market.get(market_name, deque())
        return list(fills)[-n:]

    def count(self) -> int:
        return len(self._fills)

    @property
    def last(self) -> Optional[Fill]:
        return self._fills[-1] if self._fills else None
