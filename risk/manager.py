"""Risk manager — kill switch, limits, sanity checks."""

from __future__ import annotations

import time
import structlog

from core.types import BotConfig, Intent, IntentType

logger = structlog.get_logger()


class RiskManager:
    """Central risk manager across all markets."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.daily_pnl: float = 0.0
        self.reject_count: int = 0
        self.consecutive_losses: int = 0
        self.stale_count: int = 0
        self.cancel_timestamps: list[float] = []
        self.is_killed: bool = False
        self.kill_reason: str = ""
        self.kill_ts: float = 0.0

    def check_kill(self) -> bool:
        """Check if kill switch should trigger."""
        if self.is_killed:
            return True

        reasons = []

        if self.daily_pnl < self.cfg.max_daily_loss:
            reasons.append(f"daily_pnl={self.daily_pnl:.2f} < {self.cfg.max_daily_loss}")

        if self.reject_count > self.cfg.max_rejects:
            reasons.append(f"rejects={self.reject_count} > {self.cfg.max_rejects}")

        if self.consecutive_losses > self.cfg.max_consecutive_losses:
            reasons.append(f"consec_losses={self.consecutive_losses}")

        if reasons:
            self.is_killed = True
            self.kill_reason = "; ".join(reasons)
            self.kill_ts = time.time()
            logger.critical("kill_switch", reason=self.kill_reason)
            return True

        return False

    def can_cancel(self) -> bool:
        """Check if we're within cancel rate limit."""
        now = time.time()
        # Remove old timestamps (> 60s ago)
        self.cancel_timestamps = [
            ts for ts in self.cancel_timestamps if now - ts < 60
        ]
        return len(self.cancel_timestamps) < self.cfg.max_cancel_per_min

    def record_cancel(self):
        """Record a cancel for rate limiting."""
        self.cancel_timestamps.append(time.time())

    def record_reject(self):
        """Record an order reject."""
        self.reject_count += 1

    def record_fill_pnl(self, pnl: float):
        """Record PnL from a fill."""
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def record_stale_book(self):
        """Record a stale book event."""
        self.stale_count += 1

    def filter_intents(self, intents: list[Intent]) -> list[Intent]:
        """Filter intents through risk checks."""
        if self.check_kill():
            # Kill switch: only allow cancels
            return [Intent(
                type=IntentType.KILL_SWITCH,
                market_name="ALL",
                reason=self.kill_reason,
            )]

        filtered = []
        for intent in intents:
            if intent.type == IntentType.CANCEL_ORDER:
                if self.can_cancel():
                    self.record_cancel()
                    filtered.append(intent)
                else:
                    logger.warning("cancel_rate_limited", market=intent.market_name)
            elif intent.type == IntentType.CANCEL_ALL:
                filtered.append(intent)
            else:
                filtered.append(intent)

        return filtered

    def reset_daily(self):
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.reject_count = 0
        self.consecutive_losses = 0
        self.stale_count = 0
        self.is_killed = False
        self.kill_reason = ""

    def status(self) -> dict:
        """Return current risk status."""
        return {
            "daily_pnl": self.daily_pnl,
            "reject_count": self.reject_count,
            "consecutive_losses": self.consecutive_losses,
            "stale_count": self.stale_count,
            "is_killed": self.is_killed,
            "kill_reason": self.kill_reason,
            "cancels_last_min": len(self.cancel_timestamps),
        }
