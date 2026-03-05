"""Core decision engine — state machine + grid intent generation.

Key flow per tick:
1. Update time regime
2. Check state transitions (IDLE/QUOTING/REBALANCING/EXITING)
3. Compute desired grid quotes
4. Cancel seletivo: so cancela niveis que mudaram de preco
5. Coloca novos niveis que estao vazios
6. Retorna intents (cancel + place)

Grid dinamico:
- EARLY: 1 nivel por lado (cauteloso)
- MID:   5 niveis por lado (grid completo)
- LATE:  0 BUY, 5 SELL (so desova)
- EXIT:  cancela tudo, vende a mercado se necessario
"""

from __future__ import annotations

import time
import structlog
from typing import TYPE_CHECKING

from core.types import (
    BotConfig, BotState, Direction, Intent, IntentType,
    MarketState, Quote, Side, TimeRegime,
)
from core.quoter import compute_all_quotes
from core.pair import check_pair

if TYPE_CHECKING:
    from execution.order_manager import OrderManager

logger = structlog.get_logger()


class Engine:
    """Decision engine for one market.

    Pure logic — no I/O. Returns intents that the executor handles.
    """

    def __init__(self, market: MarketState, cfg: BotConfig):
        self.market = market
        self.cfg = cfg
        self._last_quote_ts: float = 0.0
        self._requote_requested: bool = False

    def update_regime(self):
        """Update time regime based on remaining time."""
        t = self.market.time_remaining_s
        if t > self.cfg.t_early:
            self.market.regime = TimeRegime.EARLY
        elif t > self.cfg.t_mid:
            self.market.regime = TimeRegime.MID
        elif t > self.cfg.t_late:
            self.market.regime = TimeRegime.LATE
        else:
            self.market.regime = TimeRegime.EXIT

    def tick(self, live_order_ids: list[str], order_mgr: "OrderManager") -> list[Intent]:
        """Run one decision cycle. Returns list of intents.

        Args:
            live_order_ids: IDs das ordens vivas para este mercado
            order_mgr: OrderManager para lookup de nivel por order_id (cancel seletivo)
        """
        intents: list[Intent] = []
        now = time.time()

        self.update_regime()

        # Check cooldown
        if now < self.market.cooldown_until:
            return intents

        state = self.market.state
        regime = self.market.regime
        inv = self.market.inventory
        book_up = self.market.book_up
        book_down = self.market.book_down

        # === IDLE: verifica se pode comecar ===
        if state == BotState.IDLE:
            if self._can_start():
                self.market.state = BotState.QUOTING
                logger.info("state_change", market=self.market.name,
                           from_state="IDLE", to_state="QUOTING",
                           regime=regime.value)
            return intents

        # === EXIT regime: forca saida ===
        if regime == TimeRegime.EXIT:
            if state != BotState.EXITING:
                self.market.state = BotState.EXITING
                intents.extend(self._cancel_all_intents(live_order_ids, "time_exit"))
                logger.info("state_change", market=self.market.name,
                           to_state="EXITING",
                           time_remaining=f"{self.market.time_remaining_s:.0f}s")
            intents.extend(self._exit_intents())
            return intents

        # === EXITING (kill switch etc.) ===
        if state == BotState.EXITING:
            intents.extend(self._exit_intents())
            if abs(inv.net) < 1.0:
                self.market.state = BotState.IDLE
            return intents

        # === Hard limit: para de cotar ===
        if abs(inv.net) > self.cfg.net_hard_limit:
            intents.extend(self._cancel_all_intents(live_order_ids, "net_hard_limit"))
            logger.warning("hard_limit_breached", market=self.market.name, net=inv.net)
            return intents

        # === Transicoes REBALANCING ===
        if abs(inv.net) > self.cfg.net_soft_limit:
            if state != BotState.REBALANCING:
                self.market.state = BotState.REBALANCING
                logger.info("state_change", market=self.market.name,
                           to_state="REBALANCING", net=inv.net)
        elif state == BotState.REBALANCING:
            if abs(inv.net) < self.cfg.net_soft_limit * 0.5:
                self.market.state = BotState.QUOTING
                logger.info("state_change", market=self.market.name,
                           to_state="QUOTING", net=inv.net)

        # === Par/arb ===
        pair_signal = check_pair(book_up, book_down, self.cfg)
        if pair_signal:
            logger.info("pair_detected", market=self.market.name,
                       edge=pair_signal.edge, direction=pair_signal.direction)
            intents.extend(self._pair_intents(pair_signal))

        # === Throttle re-quoting ===
        min_interval = self.cfg.quote_ttl_ms / 1000.0 * 0.8
        if now - self._last_quote_ts < min_interval and not self._requote_requested:
            return intents

        self._requote_requested = False

        # === Validade do book ===
        if book_up.is_stale(self.cfg.stale_book_ms) or book_down.is_stale(self.cfg.stale_book_ms):
            return intents

        # === Grid: calcula quotes desejadas ===
        new_quotes = compute_all_quotes(book_up, book_down, inv, regime, self.cfg)

        # === Cancel seletivo: so cancela niveis que mudaram de preco ===
        ids_to_cancel = self._selective_cancel(live_order_ids, new_quotes, order_mgr)
        if ids_to_cancel:
            intents.extend(self._cancel_intents(ids_to_cancel, "grid_reprice"))

        # === Coloca niveis que nao tem ordem viva ===
        # Monta conjunto dos niveis ocupados (excluindo os que serao cancelados)
        occupied: set[tuple] = set()
        for oid in live_order_ids:
            if oid in ids_to_cancel:
                continue
            order = order_mgr.get(oid)
            if order:
                occupied.add((order.side, order.direction, order.level))

        for q in new_quotes:
            key = (q.side, q.direction, q.level)
            if key not in occupied:
                intents.append(Intent(
                    type=IntentType.PLACE_ORDER,
                    market_name=self.market.name,
                    side=q.side,
                    direction=q.direction,
                    price=q.price,
                    size=q.size,
                    level=q.level,
                ))

        if new_quotes:
            self._last_quote_ts = now

        return intents

    def request_requote(self):
        """Solicita re-quote imediato (ex: apos fill)."""
        self._requote_requested = True

    def transition(self, new_state: BotState):
        """Forca transicao de estado."""
        old = self.market.state
        self.market.state = new_state
        logger.info("state_change", market=self.market.name,
                   from_state=old.value, to_state=new_state.value)

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _can_start(self) -> bool:
        if not self.market.is_active:
            return False
        if self.market.time_remaining_s <= self.cfg.t_exit:
            return False
        if not self.market.book_up.is_valid:
            return False
        if not self.market.book_down.is_valid:
            return False
        return True

    def _selective_cancel(
        self,
        live_order_ids: list[str],
        new_quotes: list[Quote],
        order_mgr: "OrderManager",
    ) -> list[str]:
        """Retorna IDs a cancelar: so niveis que mudaram de preco ou sumiram do grid.

        Nao cancela ordens cujo nivel ainda existe e o preco nao mudou.
        Isso reduz drasticamente o numero de cancels no grid (vs cancelar tudo).
        """
        threshold = self.cfg.price_move_threshold  # 0.01 = 1 tick
        to_cancel: list[str] = []

        # Indexa quotes desejadas por (side, direction, level)
        desired: dict[tuple, Quote] = {
            (q.side, q.direction, q.level): q
            for q in new_quotes
        }

        for oid in live_order_ids:
            order = order_mgr.get(oid)
            if order is None:
                to_cancel.append(oid)
                continue

            key = (order.side, order.direction, order.level)
            target = desired.get(key)

            if target is None:
                # Nivel nao existe mais no novo grid (regime mudou, inventario zerou, etc.)
                to_cancel.append(oid)
            elif abs(target.price - order.price) >= threshold:
                # Preco do nivel mudou >= 1 tick: cancela e recoloca
                to_cancel.append(oid)
            # Caso contrario: nivel ainda valido, nao cancela

        return to_cancel

    def _cancel_intents(self, order_ids: list[str], reason: str) -> list[Intent]:
        return [
            Intent(
                type=IntentType.CANCEL_ORDER,
                market_name=self.market.name,
                order_id=oid,
                reason=reason,
            )
            for oid in order_ids
        ]

    def _cancel_all_intents(self, order_ids: list[str], reason: str) -> list[Intent]:
        return self._cancel_intents(order_ids, reason)

    def _exit_intents(self) -> list[Intent]:
        """Gera intents de saida: vende posicao a mercado (taker)."""
        intents = []
        inv = self.market.inventory

        if inv.net > 1.0 and self.market.book_up.is_valid:
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.UP,
                direction=Direction.SELL,
                price=self.market.book_up.best_bid,
                size=min(inv.shares_up, self.cfg.grid.level_size),
                reason="exit_reduce_up",
            ))
        elif inv.net < -1.0 and self.market.book_down.is_valid:
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.DOWN,
                direction=Direction.SELL,
                price=self.market.book_down.best_bid,
                size=min(inv.shares_down, self.cfg.grid.level_size),
                reason="exit_reduce_down",
            ))

        return intents

    def _pair_intents(self, signal) -> list[Intent]:
        intents = []
        if signal.direction == "BUY_PAIR":
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.UP, direction=Direction.BUY,
                price=signal.ask_up, size=signal.size,
                reason=f"pair_buy edge={signal.edge:.4f}",
            ))
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.DOWN, direction=Direction.BUY,
                price=signal.ask_down, size=signal.size,
                reason=f"pair_buy edge={signal.edge:.4f}",
            ))
        elif signal.direction == "SELL_PAIR":
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.UP, direction=Direction.SELL,
                price=signal.bid_up, size=signal.size,
                reason=f"pair_sell edge={signal.edge:.4f}",
            ))
            intents.append(Intent(
                type=IntentType.PLACE_ORDER,
                market_name=self.market.name,
                side=Side.DOWN, direction=Direction.SELL,
                price=signal.bid_down, size=signal.size,
                reason=f"pair_sell edge={signal.edge:.4f}",
            ))
        return intents
