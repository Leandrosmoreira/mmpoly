"""Order manager — lifecycle, cancel-on-fill, TTL management, grid level index."""

from __future__ import annotations

import time
import structlog
from collections import defaultdict
from typing import Optional

from core.types import (
    BotConfig, Direction, Fill, Intent, IntentType,
    LiveOrder, Side,
)

logger = structlog.get_logger()


class OrderManager:
    """Manages live orders and their lifecycle.

    Indice de grid:
      _grid[market_name][side][direction][level] = order_id
      Permite ao Engine verificar quais niveis ja tem ordem viva,
      sem precisar iterar por todos os IDs.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._orders: dict[str, LiveOrder] = {}
        self._by_market: dict[str, set[str]] = {}
        self._by_token: dict[str, set[str]] = {}
        # Indice grid: [market][side_str][dir_str][level] = order_id
        self._grid: dict[str, dict[str, dict[str, dict[int, str]]]] = {}

    def register(self, order: LiveOrder):
        """Registra nova ordem viva."""
        self._orders[order.order_id] = order
        self._by_market.setdefault(order.market_name, set()).add(order.order_id)
        self._by_token.setdefault(order.token_id, set()).add(order.order_id)

        # Atualiza indice grid
        (self._grid
            .setdefault(order.market_name, {})
            .setdefault(order.side.value, {})
            .setdefault(order.direction.value, {})
            [order.level]) = order.order_id

        logger.debug("order_registered", order_id=order.order_id,
                     market=order.market_name, side=order.side.value,
                     direction=order.direction.value, px=order.price,
                     sz=order.size, level=order.level)

    def remove(self, order_id: str):
        """Remove ordem (filled ou cancelada)."""
        order = self._orders.pop(order_id, None)
        if order:
            market_set = self._by_market.get(order.market_name)
            if market_set:
                market_set.discard(order_id)
            token_set = self._by_token.get(order.token_id)
            if token_set:
                token_set.discard(order_id)

            # Limpa indice grid
            try:
                lvl_map = (self._grid
                           .get(order.market_name, {})
                           .get(order.side.value, {})
                           .get(order.direction.value, {}))
                if lvl_map.get(order.level) == order_id:
                    lvl_map.pop(order.level, None)
            except (KeyError, AttributeError):
                pass

    def get(self, order_id: str) -> Optional[LiveOrder]:
        """Lookup de ordem por ID."""
        return self._orders.get(order_id)

    def get_order_ids_for_market(self, market_name: str) -> list[str]:
        """IDs de todas as ordens vivas para um mercado."""
        return list(self._by_market.get(market_name, set()))

    def get_level_order_id(
        self,
        market_name: str,
        side: Side,
        direction: Direction,
        level: int,
    ) -> Optional[str]:
        """Qual order_id esta no nivel X do grid?"""
        return (self._grid
                .get(market_name, {})
                .get(side.value, {})
                .get(direction.value, {})
                .get(level))

    def on_fill(self, fill: Fill) -> list[Intent]:
        """Processa fill e retorna intents de cancel do lado oposto.

        Cancel-on-fill: quando UP BUY e preenchido, cancela BUY DOWN.
        No grid: cancela apenas as ordens de BUY no token oposto,
        nao todas as ordens.
        """
        intents: list[Intent] = []
        order = self._orders.get(fill.order_id)

        if order:
            order.filled += fill.size
            if order.is_fully_filled:
                self.remove(fill.order_id)

        # Cancel same direction on opposite token
        for oid in list(self._orders):
            o = self._orders.get(oid)
            if o is None:
                continue
            if o.market_name != fill.market_name:
                continue
            if o.token_id == fill.token_id:
                continue
            if o.direction == fill.direction:
                intents.append(Intent(
                    type=IntentType.CANCEL_ORDER,
                    market_name=fill.market_name,
                    order_id=oid,
                    reason="cancel_on_fill",
                ))

        logger.info("fill_processed", order_id=fill.order_id,
                    market=fill.market_name, side=fill.side.value,
                    direction=fill.direction.value, px=fill.price, sz=fill.size,
                    cancel_count=len(intents))

        return intents

    def get_expired_orders(self) -> list[Intent]:
        """Encontra ordens com TTL vencido."""
        intents = []
        for order_id, order in list(self._orders.items()):
            if order.is_expired:
                intents.append(Intent(
                    type=IntentType.CANCEL_ORDER,
                    market_name=order.market_name,
                    order_id=order_id,
                    reason="ttl_expired",
                ))
        return intents

    def cancel_all_for_market(self, market_name: str) -> list[Intent]:
        order_ids = list(self._by_market.get(market_name, set()))
        return [
            Intent(type=IntentType.CANCEL_ORDER, market_name=market_name,
                   order_id=oid, reason="cancel_all_market")
            for oid in order_ids
        ]

    def cancel_all(self) -> list[Intent]:
        return [
            Intent(type=IntentType.CANCEL_ORDER,
                   market_name=self._orders[oid].market_name,
                   order_id=oid, reason="global_cancel_all")
            for oid in list(self._orders)
        ]

    def get_all_order_ids(self) -> list[str]:
        """All live order IDs across all markets. BUG-014: for reconciliation."""
        return list(self._orders.keys())

    def live_count(self, market_name: Optional[str] = None) -> int:
        if market_name:
            return len(self._by_market.get(market_name, set()))
        return len(self._orders)

    def grid_summary(self, market_name: str) -> dict:
        """Retorna resumo do grid ativo para logging."""
        summary: dict = {}
        market_grid = self._grid.get(market_name, {})
        for side_str, dirs in market_grid.items():
            for dir_str, levels in dirs.items():
                key = f"{side_str}.{dir_str}"
                summary[key] = sorted(levels.keys())
        return summary
