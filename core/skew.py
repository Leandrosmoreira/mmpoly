"""Directional price skew engine.

Computes price adjustments based on 4 components:
1. Velocity — mid-price momentum (time-windowed)
2. Imbalance — bid/ask size imbalance (time-windowed)
3. Inventory — corrective pressure to reduce net position (INVERTED sign)
4. Underlying lead — BTC price signal from Binance (deferred to Sprint 3)

Pipeline: raw_score → EMA → regime gate → time scaling → adjustments

Sign convention throughout this module:
  adj > 0 = raise price
  adj < 0 = lower price

Inventory is CORRECTIVE, not predictive:
  net > 0 (heavy UP) → component < 0 → lower sells to dump faster
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import structlog

from core.types import SkewComponents, SkewConfig, SkewResult

logger = structlog.get_logger()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class _MidSample:
    """Internal: timestamped mid-price sample."""
    mid: float
    ts: float


@dataclass
class _ImbSample:
    """Internal: timestamped bid/ask imbalance sample."""
    bid_sz: float
    ask_sz: float
    ts: float


@dataclass
class _PriceSample:
    """Internal: timestamped price for underlying."""
    price: float
    ts: float


class SkewEngine:
    """Directional price skew calculator for one token.

    Usage:
        1. On each book update: update_mid() + update_imbalance()
        2. On each BTC price update: update_underlying()
        3. On each tick: compute() → SkewResult
    """

    def __init__(self, cfg: SkewConfig):
        self.cfg = cfg
        self._mid_samples: deque[_MidSample] = deque()
        self._imb_samples: deque[_ImbSample] = deque()
        self._underlying_samples: deque[_PriceSample] = deque()
        self._ema_score: float = 0.0

    # === Data ingestion ===

    def update_mid(self, ts: float, mid: float) -> None:
        """Record a mid-price sample. Called on each book update."""
        if mid <= 0:
            return
        self._mid_samples.append(_MidSample(mid=mid, ts=ts))
        self._prune(self._mid_samples, self.cfg.price_window_seconds, ts)

    def update_imbalance(self, ts: float, bid_sz: float, ask_sz: float) -> None:
        """Record bid/ask size sample. Called on each book update."""
        self._imb_samples.append(_ImbSample(bid_sz=bid_sz, ask_sz=ask_sz, ts=ts))
        self._prune(self._imb_samples, self.cfg.imbalance_window_seconds, ts)

    def update_underlying(self, ts: float, price: float) -> None:
        """Record underlying (BTC) price sample. Called from BinanceFeed."""
        if price <= 0:
            return
        self._underlying_samples.append(_PriceSample(price=price, ts=ts))
        self._prune(self._underlying_samples, self.cfg.underlying_window_seconds, ts)

    # === Main computation ===

    def compute(self, *, net: float, soft_limit: float,
                t_remain: float, spread: float) -> SkewResult:
        """Compute skew adjustments for this tick.

        Args:
            net: inventory net position (positive = heavy UP)
            soft_limit: cfg.net_soft_limit for normalization
            t_remain: seconds remaining in the market
            spread: current book spread (for regime classification)

        Returns:
            SkewResult with reservation_adj, bid_adj, ask_adj
        """
        if not self.cfg.enabled:
            return SkewResult()

        w = self.cfg.weights

        # Compute individual components (all in [-1, +1])
        v = self._velocity()
        i = self._imbalance()
        inv_c = self._inventory(net, soft_limit)
        lead = self._underlying()

        # Weighted sum
        raw_score = (
            w.velocity * v
            + w.imbalance * i
            + w.inventory * inv_c
            + w.underlying_lead * lead
        )

        # EMA smoothing
        alpha = self.cfg.ema_alpha
        self._ema_score = alpha * raw_score + (1.0 - alpha) * self._ema_score
        smoothed = self._ema_score

        # Regime classification
        regime = self._classify_regime(smoothed, t_remain, spread)

        # Time scaling
        time_scale = self._time_scale(t_remain)

        # Regime scaling
        regime_scale = self._regime_scale(regime)

        # Final scaled score
        scaled = smoothed * time_scale * regime_scale

        # Reservation adjustment (shifts center of pricing)
        reservation_adj = _clamp(
            scaled * self.cfg.max_reservation_adj,
            -self.cfg.max_reservation_adj,
            self.cfg.max_reservation_adj,
        )

        # Side adjustments (asymmetric aggressiveness)
        bid_adj, ask_adj = self._side_adjustments(scaled, inv_c)

        return SkewResult(
            raw_score=round(raw_score, 6),
            smoothed_score=round(smoothed, 6),
            regime=regime,
            reservation_adj=round(reservation_adj, 6),
            bid_adj=round(bid_adj, 6),
            ask_adj=round(ask_adj, 6),
            components=SkewComponents(
                velocity=round(v, 4),
                imbalance=round(i, 4),
                inventory=round(inv_c, 4),
                underlying_lead=round(lead, 4),
            ),
        )

    # === Component calculations ===

    def _velocity(self) -> float:
        """Mid-price velocity over the price window.

        Positive = price rising, Negative = price falling.
        """
        if len(self._mid_samples) < 2:
            return 0.0

        oldest = self._mid_samples[0]
        newest = self._mid_samples[-1]
        dt = newest.ts - oldest.ts

        if dt < 1.0:
            return 0.0

        velocity = (newest.mid - oldest.mid) / dt
        return _clamp(velocity / self.cfg.max_velocity_per_sec, -1.0, 1.0)

    def _imbalance(self) -> float:
        """Average bid/ask size imbalance over the imbalance window.

        Positive = more bid liquidity (bullish), Negative = more ask (bearish).
        """
        if not self._imb_samples:
            return 0.0

        total_imb = 0.0
        count = 0
        for s in self._imb_samples:
            total_sz = s.bid_sz + s.ask_sz
            if total_sz > 0:
                total_imb += (s.bid_sz - s.ask_sz) / total_sz
                count += 1

        if count == 0:
            return 0.0

        avg = total_imb / count
        return _clamp(avg, -1.0, 1.0)

    def _inventory(self, net: float, soft_limit: float) -> float:
        """Corrective inventory pressure. INVERTED sign.

        net > 0 (heavy UP) → returns NEGATIVE → lower prices → dump faster.
        net < 0 (heavy DOWN) → returns POSITIVE → raise prices → dump faster.
        """
        if soft_limit <= 0:
            return 0.0
        return _clamp(-net / soft_limit, -1.0, 1.0)

    def _underlying(self) -> float:
        """Underlying (BTC) price return over the underlying window.

        Positive = BTC rising → YES should rise.
        Returns 0.0 if no data (Binance feed not connected).
        """
        if len(self._underlying_samples) < 2:
            return 0.0

        oldest = self._underlying_samples[0]
        newest = self._underlying_samples[-1]

        if oldest.price <= 0:
            return 0.0

        ret = (newest.price / oldest.price) - 1.0
        return _clamp(ret / self.cfg.max_underlying_move_pct, -1.0, 1.0)

    # === Regime & scaling ===

    def _classify_regime(self, score: float, t_remain: float, spread: float) -> str:
        """Classify market regime from score magnitude and context.

        Returns: 'flat' | 'moderate_trend' | 'strong_trend' | 'defensive'
        """
        abs_score = abs(score)

        # Defensive: spread muito largo ou pouco tempo
        if t_remain < 45.0 or spread > 0.10:
            return "defensive"

        if abs_score >= self.cfg.strong_threshold:
            return "strong_trend"
        elif abs_score >= self.cfg.lateral_threshold:
            return "moderate_trend"
        else:
            return "flat"

    def _regime_scale(self, regime: str) -> float:
        """Intensity multiplier per regime."""
        scales = {
            "flat": 0.3,
            "moderate_trend": 1.0,
            "strong_trend": 1.0,
            "defensive": 0.5,
        }
        return scales.get(regime, 1.0)

    def _time_scale(self, t_remain: float) -> float:
        """Intensity multiplier per time regime."""
        ts = self.cfg.time_scaling
        if t_remain > 300.0:
            return ts.early
        elif t_remain > 60.0:
            return ts.mid
        elif t_remain > 30.0:
            return ts.late
        else:
            return ts.exit

    # === Price adjustments ===

    def _side_adjustments(self, scaled: float, inv_component: float) -> tuple[float, float]:
        """Compute asymmetric bid/ask adjustments.

        Combines directional signal with inventory correction:
        - Directional: both sides follow the signal
        - Inventory: pushes bid away from heavy side, pulls ask closer

        Returns: (bid_adj, ask_adj)
        """
        max_adj = self.cfg.max_side_adj

        # Directional: 50% of signal
        dir_adj = 0.5 * scaled * max_adj

        # Inventory corrective: 50% of signal
        # inv_component < 0 (heavy UP) → bid_adj more negative, ask_adj less negative
        inv_adj = 0.5 * inv_component * max_adj

        bid_adj = _clamp(dir_adj + inv_adj, -max_adj, max_adj)
        ask_adj = _clamp(dir_adj - inv_adj, -max_adj, max_adj)

        return round(bid_adj, 6), round(ask_adj, 6)

    # === Buffer management ===

    @staticmethod
    def _prune(buf: deque, window_s: float, now: float) -> None:
        """Remove samples older than window_s from the buffer."""
        cutoff = now - window_s
        while buf and buf[0].ts < cutoff:
            buf.popleft()
