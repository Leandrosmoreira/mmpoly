"""Tests for BUG-019: kill switch not triggering during intent execution.

The bug: filter_intents() checks daily_pnl BEFORE execution. Fills happen
inside _execute_intents() which updates PnL AFTER the check. Kill switch
only fires on the next tick (+0.5s), allowing more orders to execute.

Fix: re-check kill switch after each fill inside _execute_intents().
When triggered, drain remaining PLACE_ORDER intents and inject KILL_SWITCH.

Also fixes boundary: daily_pnl == max_daily_loss now triggers (was strict <).
"""

import pytest
from risk.manager import RiskManager
from core.types import BotConfig, Intent, IntentType


class TestKillSwitchBoundary:
    """BUG-019: boundary condition — exactly at threshold should trigger."""

    def test_exactly_at_threshold_triggers(self):
        """daily_pnl == max_daily_loss should trigger kill switch."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -3.0
        assert rm.check_kill() is True

    def test_above_threshold_no_kill(self):
        """daily_pnl just above threshold should NOT trigger."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -2.99
        assert rm.check_kill() is False

    def test_below_threshold_triggers(self):
        """daily_pnl below threshold should trigger."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -3.01
        assert rm.check_kill() is True


class TestKillAfterFill:
    """BUG-019: kill switch fires mid-execution when fill pushes past threshold."""

    def test_fill_pushes_past_threshold(self):
        """A fill that pushes daily_pnl past threshold should be detected."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -2.80  # under threshold
        assert rm.check_kill() is False

        # Fill with -0.25 loss
        rm.record_fill_pnl(-0.25)
        assert rm.daily_pnl == -3.05
        assert rm.check_kill() is True

    def test_positive_fill_no_kill(self):
        """Profitable fill should not trigger kill."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -2.80
        rm.record_fill_pnl(0.50)
        assert rm.daily_pnl == -2.30
        assert rm.check_kill() is False

    def test_kill_is_sticky(self):
        """Once triggered, kill stays on even if PnL improves."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -3.50
        rm.check_kill()
        assert rm.is_killed is True
        # PnL improves
        rm.daily_pnl = -1.0
        assert rm.check_kill() is True  # still killed

    def test_filter_intents_returns_kill(self):
        """filter_intents should return KILL_SWITCH when killed."""
        cfg = BotConfig(max_daily_loss=-3.0)
        rm = RiskManager(cfg)
        rm.daily_pnl = -3.50

        intents = [
            Intent(type=IntentType.PLACE_ORDER, market_name="test",
                   price=0.50, size=5.0),
        ]
        result = rm.filter_intents(intents)
        assert len(result) == 1
        assert result[0].type == IntentType.KILL_SWITCH


class TestConsecutiveLosses:
    """Kill switch via consecutive losses threshold."""

    def test_consecutive_losses_trigger(self):
        """Exceeding max_consecutive_losses triggers kill."""
        cfg = BotConfig(max_consecutive_losses=5)
        rm = RiskManager(cfg)
        for _ in range(6):
            rm.record_fill_pnl(-0.10)
        assert rm.consecutive_losses == 6
        assert rm.check_kill() is True

    def test_profit_resets_streak(self):
        """A profitable fill resets consecutive_losses."""
        cfg = BotConfig(max_consecutive_losses=5)
        rm = RiskManager(cfg)
        for _ in range(4):
            rm.record_fill_pnl(-0.10)
        assert rm.consecutive_losses == 4
        rm.record_fill_pnl(0.05)
        assert rm.consecutive_losses == 0

    def test_at_threshold_no_kill(self):
        """Exactly at max_consecutive_losses should NOT trigger (uses >)."""
        cfg = BotConfig(max_consecutive_losses=5)
        rm = RiskManager(cfg)
        for _ in range(5):
            rm.record_fill_pnl(-0.10)
        assert rm.consecutive_losses == 5
        assert rm.check_kill() is False  # 5 > 5 is False
