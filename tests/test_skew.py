"""Tests for directional price skew engine (core/skew.py)."""

import time
import pytest

from core.types import SkewConfig, SkewWeights, SkewTimeScaling, SkewResult, SkewComponents
from core.skew import SkewEngine


@pytest.fixture
def skew_cfg() -> SkewConfig:
    """Default skew config for tests."""
    return SkewConfig(
        enabled=True,
        shadow_mode=False,
        price_window_seconds=20.0,
        imbalance_window_seconds=8.0,
        underlying_window_seconds=45.0,
        ema_alpha=0.22,
        weights=SkewWeights(
            velocity=0.25,
            imbalance=0.20,
            inventory=0.20,
            underlying_lead=0.35,
        ),
        max_velocity_per_sec=0.004,
        max_underlying_move_pct=0.0025,
        max_reservation_adj=0.01,
        max_side_adj=0.005,
        lateral_threshold=0.12,
        strong_threshold=0.35,
        time_scaling=SkewTimeScaling(early=0.40, mid=0.70, late=1.00, exit=0.00),
    )


@pytest.fixture
def engine(skew_cfg) -> SkewEngine:
    return SkewEngine(skew_cfg)


# === Component: Inventory ===

class TestInventoryComponent:
    """Inventory component: INVERTED sign, corrective only."""

    def test_heavy_up_returns_negative(self, engine):
        """net > 0 (heavy UP) → component < 0 (wants to exit UP)."""
        result = engine.compute(net=10.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.inventory < 0
        assert result.components.inventory == pytest.approx(-1.0)

    def test_heavy_down_returns_positive(self, engine):
        """net < 0 (heavy DOWN) → component > 0 (wants to exit DOWN)."""
        result = engine.compute(net=-10.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.inventory > 0
        assert result.components.inventory == pytest.approx(1.0)

    def test_neutral_returns_zero(self, engine):
        """net = 0 → component = 0 (no correction needed)."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.inventory == pytest.approx(0.0)

    def test_clamped_to_range(self, engine):
        """Extreme net is clamped to [-1, +1]."""
        result = engine.compute(net=100.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.inventory == pytest.approx(-1.0)


# === Component: Velocity ===

class TestVelocityComponent:
    """Velocity component: mid-price momentum."""

    def test_rising_mid_positive(self, engine):
        """Mid going up → velocity > 0."""
        now = time.time()
        # Simulate mid rising from 0.50 to 0.52 over 10s
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50 + i * 0.002)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.velocity > 0

    def test_falling_mid_negative(self, engine):
        """Mid going down → velocity < 0."""
        now = time.time()
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50 - i * 0.002)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.velocity < 0

    def test_flat_mid_zero(self, engine):
        """Stable mid → velocity ≈ 0."""
        now = time.time()
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.velocity == pytest.approx(0.0, abs=0.01)

    def test_no_samples_returns_zero(self, engine):
        """No mid samples → velocity = 0."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.velocity == pytest.approx(0.0)


# === Component: Imbalance ===

class TestImbalanceComponent:
    """Imbalance component: bid/ask size ratio."""

    def test_bid_heavy_positive(self, engine):
        """More bid liquidity → imbalance > 0 (bullish)."""
        now = time.time()
        for i in range(5):
            engine.update_imbalance(now - 4 + i, bid_sz=200.0, ask_sz=50.0)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.imbalance > 0

    def test_ask_heavy_negative(self, engine):
        """More ask liquidity → imbalance < 0 (bearish)."""
        now = time.time()
        for i in range(5):
            engine.update_imbalance(now - 4 + i, bid_sz=50.0, ask_sz=200.0)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.imbalance < 0

    def test_balanced_near_zero(self, engine):
        """Equal bid/ask → imbalance ≈ 0."""
        now = time.time()
        for i in range(5):
            engine.update_imbalance(now - 4 + i, bid_sz=100.0, ask_sz=100.0)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.imbalance == pytest.approx(0.0, abs=0.01)

    def test_no_samples_returns_zero(self, engine):
        """No imbalance samples → imbalance = 0."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.imbalance == pytest.approx(0.0)


# === Component: Underlying Lead ===

class TestUnderlyingLeadComponent:
    """Underlying lead component: BTC price signal."""

    def test_btc_rising_positive(self, engine):
        """BTC going up → underlying > 0."""
        now = time.time()
        engine.update_underlying(now - 30, 60000.0)
        engine.update_underlying(now, 60200.0)  # ~0.33% up
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.underlying_lead > 0

    def test_btc_falling_negative(self, engine):
        """BTC going down → underlying < 0."""
        now = time.time()
        engine.update_underlying(now - 30, 60000.0)
        engine.update_underlying(now, 59800.0)  # ~0.33% down
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.underlying_lead < 0

    def test_no_btc_returns_zero(self, engine):
        """No underlying data → underlying = 0 (safe default)."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.components.underlying_lead == pytest.approx(0.0)


# === Time Scaling ===

class TestTimeScaling:
    """Time-dependent skew intensity."""

    def test_exit_regime_zero_effect(self, engine):
        """t_remain < 30s (EXIT) → time_scale = 0 → all adjustments zero."""
        now = time.time()
        # Strong signal: mid rising fast
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50 + i * 0.003)
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=10.0, spread=0.05)
        assert result.reservation_adj == pytest.approx(0.0)
        assert result.bid_adj == pytest.approx(0.0)
        assert result.ask_adj == pytest.approx(0.0)

    def test_late_regime_max_scaling(self, engine):
        """t_remain in LATE (30-60s) → time_scale = 1.0 (maximum)."""
        now = time.time()
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50 + i * 0.003)
        result_late = engine.compute(net=0.0, soft_limit=10.0, t_remain=45.0, spread=0.05)

        engine2 = SkewEngine(engine.cfg)
        for i in range(11):
            engine2.update_mid(now - 10 + i, 0.50 + i * 0.003)
        result_early = engine2.compute(net=0.0, soft_limit=10.0, t_remain=400.0, spread=0.05)

        # LATE scaling (1.0) should produce larger adjustments than EARLY (0.4)
        assert abs(result_late.reservation_adj) >= abs(result_early.reservation_adj)


# === Regime Classification ===

class TestRegimeClassification:
    """Regime classification from score + context."""

    def test_defensive_low_time(self, engine):
        """t_remain < 45s → defensive."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=30.0, spread=0.05)
        assert result.regime == "defensive"

    def test_defensive_wide_spread(self, engine):
        """Spread > 0.10 → defensive."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.15)
        assert result.regime == "defensive"

    def test_flat_when_no_signal(self, engine):
        """No data → score near 0 → flat."""
        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.regime == "flat"


# === SkewResult Structure ===

class TestSkewResult:
    """SkewResult output format and bounds."""

    def test_disabled_returns_empty(self):
        """Disabled config → empty SkewResult."""
        cfg = SkewConfig(enabled=False)
        engine = SkewEngine(cfg)
        result = engine.compute(net=5.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        assert result.reservation_adj == 0.0
        assert result.bid_adj == 0.0
        assert result.ask_adj == 0.0
        assert result.regime == "flat"

    def test_reservation_adj_bounded(self, engine):
        """reservation_adj never exceeds max_reservation_adj."""
        now = time.time()
        # Strong signal in all components
        for i in range(20):
            engine.update_mid(now - 19 + i, 0.40 + i * 0.005)
            engine.update_imbalance(now - 19 + i, bid_sz=500.0, ask_sz=10.0)
        engine.update_underlying(now - 30, 50000.0)
        engine.update_underlying(now, 51000.0)

        # Multiple computes to saturate EMA
        for _ in range(20):
            result = engine.compute(net=-10.0, soft_limit=10.0, t_remain=45.0, spread=0.05)

        assert abs(result.reservation_adj) <= engine.cfg.max_reservation_adj + 1e-9

    def test_side_adj_bounded(self, engine):
        """bid_adj and ask_adj never exceed max_side_adj."""
        now = time.time()
        for i in range(20):
            engine.update_mid(now - 19 + i, 0.40 + i * 0.005)

        for _ in range(20):
            result = engine.compute(net=-10.0, soft_limit=10.0, t_remain=45.0, spread=0.05)

        assert abs(result.bid_adj) <= engine.cfg.max_side_adj + 1e-9
        assert abs(result.ask_adj) <= engine.cfg.max_side_adj + 1e-9


# === Buffer Pruning ===

class TestBufferPruning:
    """Time-windowed buffer management."""

    def test_old_samples_pruned(self, engine):
        """Samples older than window are pruned."""
        now = time.time()
        # Add old sample (outside 20s window)
        engine.update_mid(now - 30, 0.50)
        # Add recent sample (inside window)
        engine.update_mid(now, 0.52)
        # Old sample should have been pruned
        assert len(engine._mid_samples) == 1
        assert engine._mid_samples[0].mid == 0.52

    def test_imbalance_window_shorter(self, engine):
        """Imbalance uses shorter window (8s vs 20s for velocity)."""
        now = time.time()
        # Add sample 10s ago — within velocity window but outside imbalance window
        engine.update_imbalance(now - 10, bid_sz=100.0, ask_sz=50.0)
        engine.update_imbalance(now, bid_sz=100.0, ask_sz=50.0)
        # Only the recent sample should survive the 8s window
        assert len(engine._imb_samples) == 1


# === EMA Smoothing ===

class TestEMASmoothing:
    """EMA smoothing of raw score."""

    def test_ema_smooths_signal(self, engine):
        """EMA makes smoothed_score lag behind raw_score."""
        now = time.time()
        # Strong signal
        for i in range(11):
            engine.update_mid(now - 10 + i, 0.50 + i * 0.003)

        result = engine.compute(net=0.0, soft_limit=10.0, t_remain=120.0, spread=0.05)
        # First compute: smoothed should be dampened vs raw
        # (EMA starts at 0, alpha=0.22 means first value is 22% of raw)
        if result.raw_score != 0:
            assert abs(result.smoothed_score) < abs(result.raw_score)
