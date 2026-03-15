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


@dataclass
class SomaConfig:
    """Soma check config (UP_mid + DOWN_mid ≈ 1.0 mispricing detection)."""
    enabled: bool = True
    fair_value: float = 1.0            # soma esperada
    threshold: float = 0.03            # divergencia minima para ativar (3 ticks)
    max_adjustment: float = 0.03       # ajuste maximo por lado (3 ticks)
    aggression: float = 0.5            # 0.0-1.0: fracao da divergencia usada como offset


# === Skew Config ===

@dataclass
class SkewWeights:
    """Component weights for directional skew score. Must sum to ~1.0."""
    velocity: float = 0.25             # local mid-price momentum
    imbalance: float = 0.20            # bid/ask size imbalance
    inventory: float = 0.20            # corrective inventory pressure
    underlying_lead: float = 0.35      # BTC lead signal (highest weight for BTC 15m)


@dataclass
class SkewTimeScaling:
    """Skew intensity multiplier per time regime."""
    early: float = 0.40                # cauteloso, pouca confianca
    mid: float = 0.70                  # grid completo, skew moderado
    late: float = 1.00                 # maximo, precisa descarregar rapido
    exit: float = 0.00                 # sem skew, so desova


@dataclass
class SkewConfig:
    """Directional price skew indicator config.

    Adjusts bid/ask prices based on momentum, inventory pressure,
    book imbalance, and underlying lead signal (BTC via Binance).

    Sign convention: adj > 0 = raise price, adj < 0 = lower price.
    """
    enabled: bool = False              # desligado por default (seguro)
    shadow_mode: bool = True           # calcula sem afetar ordens

    # Janelas temporais por componente
    price_window_seconds: float = 20.0        # velocity window
    imbalance_window_seconds: float = 8.0     # book imbalance (mais rapido)
    underlying_window_seconds: float = 45.0   # BTC (mais suave)

    ema_alpha: float = 0.22            # EMA smoothing (~4 amostras efetivas)

    weights: SkewWeights = field(default_factory=SkewWeights)

    # Normalization caps
    max_velocity_per_sec: float = 0.004       # 0.4c/s = sinal maximo
    max_underlying_move_pct: float = 0.0025   # 0.25%/min no BTC = sinal maximo

    # Price adjustment limits
    max_reservation_adj: float = 0.01         # 1 tick max para deslocar centro
    max_side_adj: float = 0.005               # 0.5 tick max para agressividade por lado

    # Regime thresholds
    lateral_threshold: float = 0.12           # |score| abaixo = flat → scale 0.3x
    strong_threshold: float = 0.35            # |score| acima = strong_trend

    time_scaling: SkewTimeScaling = field(default_factory=SkewTimeScaling)


@dataclass
class SkewComponents:
    """Individual component values of the skew score (all in [-1, +1])."""
    velocity: float = 0.0
    imbalance: float = 0.0
    inventory: float = 0.0
    underlying_lead: float = 0.0


@dataclass
class SkewResult:
    """Output of SkewEngine.compute(). Used by quoter for price adjustments.

    Sign convention: adj > 0 = raise price, adj < 0 = lower price.
    """
    raw_score: float = 0.0
    smoothed_score: float = 0.0
    regime: str = "flat"               # flat | moderate_trend | strong_trend | defensive
    reservation_adj: float = 0.0       # desloca centro (bid E ask)
    bid_adj: float = 0.0              # ajuste adicional para BUY
    ask_adj: float = 0.0              # ajuste adicional para SELL
    components: SkewComponents = field(default_factory=SkewComponents)


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

    @property
    def has_bid(self) -> bool:
        """Has a non-zero bid price — sufficient for placing sell orders."""
        return self.best_bid > 0

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

    # BUG-026: Per-side loss tracking — block re-buy after selling at loss
    side_realized_up: float = 0.0    # cumulative realized PnL on UP side
    side_realized_down: float = 0.0  # cumulative realized PnL on DOWN side
    buy_blocked_up: bool = False     # True = stop buying UP (sold at loss)
    buy_blocked_down: bool = False   # True = stop buying DOWN (sold at loss)

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
                    delta = (px - self.avg_cost_up) * sz
                    self.realized_pnl += delta
                    self.side_realized_up += delta
                    # BUG-026: Block re-buy if this side is losing
                    if self.side_realized_up < -0.50:
                        self.buy_blocked_up = True
                self.shares_up = max(0, self.shares_up - sz)
        else:  # DOWN
            if direction == Direction.BUY:
                total_cost = self.avg_cost_down * self.shares_down + px * sz
                self.shares_down += sz
                self.avg_cost_down = total_cost / self.shares_down if self.shares_down > 0 else 0
            else:  # SELL
                if self.shares_down > 0:
                    delta = (px - self.avg_cost_down) * sz
                    self.realized_pnl += delta
                    self.side_realized_down += delta
                    # BUG-026: Block re-buy if this side is losing
                    if self.side_realized_down < -0.50:
                        self.buy_blocked_down = True
                self.shares_down = max(0, self.shares_down - sz)

    def unrealized_pnl(self, mid_up: float, mid_down: float) -> float:
        """Compute unrealized PnL at current mid prices.

        BUG-016: Used by engine to detect adverse moves and trigger
        emergency sells before loss grows too large.
        """
        pnl = 0.0
        if self.shares_up > 0 and mid_up > 0:
            pnl += (mid_up - self.avg_cost_up) * self.shares_up
        if self.shares_down > 0 and mid_down > 0:
            pnl += (mid_down - self.avg_cost_down) * self.shares_down
        return pnl


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
    levels: int = 5                # alias para grid.max_levels

    # Grid dinamico
    grid_levels: int = 0               # 0=usa grid: block, 1/3/5=atalho simples
    grid: GridConfig = field(default_factory=GridConfig)
    price_move_threshold: float = 0.01  # cancela nivel se preco mudou >= 1 tick

    # Soma check (mispricing UP+DOWN)
    soma: SomaConfig = field(default_factory=SomaConfig)

    # Directional skew indicator
    skew: SkewConfig = field(default_factory=SkewConfig)

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

    # BUG-016: Adverse movement — emergency sell when losing too much
    adverse_loss_threshold: float = -0.20   # USDC unrealized loss to trigger emergency sell
    adverse_sell_at_bid: bool = True         # sell at bid (fast fill) vs ask-tick (slower)

    # BUG-033/034/035: Adverse sell improvements
    adverse_max_fok_attempts: int = 3       # max FOK attempts before switching to POST_ONLY
    adverse_cooldown_s: float = 60.0        # cooldown after adverse sell (prevents re-entry)
    adverse_max_loss_per_share: float = 0.05  # max loss per share on emergency sell

    # BUG-025: Minimum price to buy a token — stop buying resolved markets
    min_buy_price: float = 0.15             # don't buy tokens below this price (market resolved)

    # Logging
    log_dir: str = "logs"
    snapshot_interval_s: float = 30.0

    # Mode
    dry_run: bool = True
