"""Core data types for GabaBook MM Bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# === Enums ===

class Side(str, Enum):
    UP = "UP"      # YES token
    DOWN = "DOWN"  # NO token


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeRegime(str, Enum):
    EARLY = "EARLY"
    MID = "MID"
    LATE = "LATE"
    EXIT = "EXIT"


class BotState(str, Enum):
    IDLE = "IDLE"
    QUOTING = "QUOTING"
    REBALANCING = "REBALANCING"
    PAIR_OPPORTUNITY = "PAIR_OPPORTUNITY"
    EXITING = "EXITING"


class IntentType(str, Enum):
    PLACE_ORDER = "PLACE_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    CANCEL_ALL = "CANCEL_ALL"
    SET_COOLDOWN = "SET_COOLDOWN"
    KILL_SWITCH = "KILL_SWITCH"


# === Grid Config ===

@dataclass
class GridConfig:
    """Dynamic grid configuration."""
    max_levels: int = 5                # max niveis por lado por token
    level_spacing_ticks: int = 2       # ticks entre niveis (2 ticks = 0.02)
    level_size: float = 5.0            # shares por nivel (minimo Poly = 5)

    # Niveis ativos por regime
    early_buy_levels: int = 1          # EARLY: 1 nivel de compra (cauteloso)
    early_sell_levels: int = 1         # EARLY: 1 nivel de venda
    mid_buy_levels: int = 5            # MID: grid completo
    mid_sell_levels: int = 5


# === Data Structures ===

@dataclass
class TopOfBook:
    """Top-of-book snapshot for one token."""
    token_id: str = ""
    best_bid: float = 0.0
    best_bid_sz: float = 0.0
    best_ask: float = 1.0
    best_ask_sz: float = 0.0
    ts: float = 0.0

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def is_valid(self) -> bool:
        return (
            self.best_bid > 0
            and self.best_ask > 0
            and self.best_ask > self.best_bid
            and self.best_bid_sz > 0
            and self.best_ask_sz > 0
        )

    def is_stale(self, max_age_ms: float) -> bool:
        age_ms = (time.time() - self.ts) * 1000
        return age_ms > max_age_ms


@dataclass
class Inventory:
    """Position tracking for one market."""
    shares_up: float = 0.0
    shares_down: float = 0.0
    avg_cost_up: float = 0.0
    avg_cost_down: float = 0.0
    realized_pnl: float = 0.0

    @property
    def net(self) -> float:
        """Positive = heavy UP, negative = heavy DOWN."""
        return self.shares_up - self.shares_down

    def apply_fill(self, side: Side, direction: Direction, px: float, sz: float):
        """Update inventory on fill."""
        if side == Side.UP:
            if direction == Direction.BUY:
                total_cost = self.avg_cost_up * self.shares_up + px * sz
                self.shares_up += sz
                self.avg_cost_up = total_cost / self.shares_up if self.shares_up > 0 else 0
            else:  # SELL
                if self.shares_up > 0:
                    self.realized_pnl += (px - self.avg_cost_up) * sz
                self.shares_up = max(0, self.shares_up - sz)
        else:  # DOWN
            if direction == Direction.BUY:
                total_cost = self.avg_cost_down * self.shares_down + px * sz
                self.shares_down += sz
                self.avg_cost_down = total_cost / self.shares_down if self.shares_down > 0 else 0
            else:  # SELL
                if self.shares_down > 0:
                    self.realized_pnl += (px - self.avg_cost_down) * sz
                self.shares_down = max(0, self.shares_down - sz)

    @property
    def unrealized_pnl_at(self) -> float:
        """Needs mid prices to compute — done externally."""
        return 0.0


@dataclass
class MarketState:
    """Full state for one market."""
    name: str
    condition_id: str
    token_up: str
    token_down: str
    book_up: TopOfBook = field(default_factory=TopOfBook)
    book_down: TopOfBook = field(default_factory=TopOfBook)
    inventory: Inventory = field(default_factory=Inventory)
    state: BotState = BotState.IDLE
    regime: TimeRegime = TimeRegime.EARLY
    end_ts: float = 0.0
    is_active: bool = True
    cooldown_until: float = 0.0

    @property
    def time_remaining_s(self) -> float:
        return max(0, self.end_ts - time.time())


@dataclass
class Quote:
    """A single quote to place."""
    side: Side
    direction: Direction
    price: float
    size: float
    level: int = 0          # grid level (0 = mais proximo do mid)
    post_only: bool = True


@dataclass
class Intent:
    """Action intent from the engine (decoupled from execution)."""
    type: IntentType
    market_name: str
    side: Optional[Side] = None
    direction: Optional[Direction] = None
    price: float = 0.0
    size: float = 0.0
    order_id: Optional[str] = None
    reason: str = ""
    level: int = 0          # grid level propagado da Quote
    ts: float = field(default_factory=time.time)


@dataclass
class Fill:
    """A fill event from the exchange."""
    order_id: str
    market_name: str
    token_id: str
    side: Side
    direction: Direction
    price: float
    size: float
    ts: float
    is_maker: bool = True


@dataclass
class LiveOrder:
    """Tracked live order."""
    order_id: str
    market_name: str
    token_id: str
    side: Side
    direction: Direction
    price: float
    size: float
    filled: float = 0.0
    placed_at: float = 0.0
    ttl_ms: float = 5000.0
    level: int = 0          # grid level (para cancel seletivo)

    @property
    def remaining(self) -> float:
        return self.size - self.filled

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.placed_at) * 1000 > self.ttl_ms

    @property
    def is_fully_filled(self) -> bool:
        return self.filled >= self.size - 1e-9


@dataclass
class BotConfig:
    """Bot configuration (loaded from YAML)."""
    # Quoting
    tick: float = 0.01
    order_size: float = 5.0        # compatibilidade (grid usa grid.level_size)
    min_spread: float = 0.02
    quote_ttl_ms: float = 5000.0
    skew_factor: float = 2.0
    levels: int = 5                # alias para grid.max_levels

    # Grid dinamico
    grid: GridConfig = field(default_factory=GridConfig)
    price_move_threshold: float = 0.01  # cancela nivel se preco mudou >= 1 tick

    # Inventory
    net_soft_limit: float = 10.0   # ajustado para grid (era 15)
    net_hard_limit: float = 25.0   # ajustado para grid (era 30)
    max_position: float = 50.0

    # Orders
    max_orders_per_side: int = 10  # 5 niveis BUY + 5 SELL por token (era 2)
    max_cancel_per_min: int = 60

    # Time regimes
    t_early: float = 300.0
    t_mid: float = 60.0
    t_late: float = 30.0
    t_exit: float = 15.0

    # Pair
    min_pair_edge: float = 0.02
    fee_buffer: float = 0.02
    max_pair_size: float = 10.0
    min_pair_size: float = 2.0

    # Risk
    max_daily_loss: float = -5.0
    stale_book_ms: float = 5000.0
    max_rejects: int = 10
    max_consecutive_losses: int = 5
    cooldown_s: float = 1800.0

    # Logging
    log_dir: str = "logs"
    snapshot_interval_s: float = 30.0

    # Mode
    dry_run: bool = True
