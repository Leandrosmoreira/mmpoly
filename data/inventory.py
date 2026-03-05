"""Inventory tracking across markets."""

from __future__ import annotations

from core.types import Direction, Fill, Inventory, Side


class InventoryTracker:
    """Track inventory per market."""

    def __init__(self):
        self._inventories: dict[str, Inventory] = {}

    def get(self, market_name: str) -> Inventory:
        if market_name not in self._inventories:
            self._inventories[market_name] = Inventory()
        return self._inventories[market_name]

    def apply_fill(self, fill: Fill):
        inv = self.get(fill.market_name)
        inv.apply_fill(fill.side, fill.direction, fill.price, fill.size)

    def total_realized_pnl(self) -> float:
        return sum(inv.realized_pnl for inv in self._inventories.values())

    def all_markets(self) -> dict[str, Inventory]:
        return dict(self._inventories)
