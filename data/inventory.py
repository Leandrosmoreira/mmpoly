"""Inventory tracking across markets."""

from __future__ import annotations

import json
import time
import structlog
from pathlib import Path

from core.types import Direction, Fill, Inventory, Side
from core.errors import ErrorCode

logger = structlog.get_logger()


class InventoryTracker:
    """Track inventory per market."""

    def __init__(self, snapshot_path: str = "logs/inventory.json"):
        self._inventories: dict[str, Inventory] = {}
        self._applied_fills: set[str] = set()
        self._snapshot_path = Path(snapshot_path)

    def get(self, market_name: str) -> Inventory:
        if market_name not in self._inventories:
            self._inventories[market_name] = Inventory()
        return self._inventories[market_name]

    def apply_fill(self, fill: Fill):
        # Idempotency: skip duplicate fills
        if fill.order_id in self._applied_fills:
            logger.warning("duplicate_fill_skipped", order_id=fill.order_id)
            return
        self._applied_fills.add(fill.order_id)
        inv = self.get(fill.market_name)
        inv.apply_fill(fill.side, fill.direction, fill.price, fill.size)
        # Persist after every fill for crash recovery
        self._save_snapshot()

    def zero_side(self, market_name: str, side: Side):
        """Zero out phantom inventory for a specific side.

        Called when the exchange rejects a SELL with "not enough balance",
        confirming that our local inventory is wrong (phantom shares).
        """
        inv = self.get(market_name)
        if side == Side.UP:
            old = inv.shares_up
            inv.shares_up = 0.0
            inv.avg_cost_up = 0.0
        else:
            old = inv.shares_down
            inv.shares_down = 0.0
            inv.avg_cost_down = 0.0
        logger.warning("phantom_inventory_zeroed",
                        market=market_name, side=side.value,
                        old_shares=old,
                        error_code=ErrorCode.PHANTOM_INVENTORY_ZEROED)
        self._save_snapshot()

    def total_realized_pnl(self) -> float:
        return sum(inv.realized_pnl for inv in self._inventories.values())

    def all_markets(self) -> dict[str, Inventory]:
        return dict(self._inventories)

    # === Persistence for crash recovery ===

    def _save_snapshot(self):
        """Save inventory snapshot to disk for crash recovery."""
        try:
            snapshot = {
                "ts": time.time(),
                "markets": {},
            }
            for name, inv in self._inventories.items():
                snapshot["markets"][name] = {
                    "shares_up": inv.shares_up,
                    "shares_down": inv.shares_down,
                    "avg_cost_up": inv.avg_cost_up,
                    "avg_cost_down": inv.avg_cost_down,
                    "realized_pnl": inv.realized_pnl,
                }
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._snapshot_path, "w") as f:
                json.dump(snapshot, f)
        except Exception as e:
            import sys
            print(f"INVENTORY_SNAPSHOT_ERROR: {e}", file=sys.stderr)

    def load_snapshot(self, max_age_s: float = 900):
        """Load inventory snapshot from disk if recent enough.

        Args:
            max_age_s: Max age in seconds (default 900 = 15 min window)
        """
        try:
            with open(self._snapshot_path) as f:
                snap = json.load(f)
            age = time.time() - snap.get("ts", 0)
            if age > max_age_s:
                logger.info("inventory_snapshot_too_old", age_s=round(age))
                return
            for name, data in snap.get("markets", {}).items():
                inv = self.get(name)
                inv.shares_up = data.get("shares_up", 0)
                inv.shares_down = data.get("shares_down", 0)
                inv.avg_cost_up = data.get("avg_cost_up", 0)
                inv.avg_cost_down = data.get("avg_cost_down", 0)
                inv.realized_pnl = data.get("realized_pnl", 0)
            logger.info("inventory_restored",
                        age_s=round(age),
                        markets=len(snap.get("markets", {})))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("inventory_load_failed", error=str(e),
                          error_code=ErrorCode.INVENTORY_LOAD_FAILED)
