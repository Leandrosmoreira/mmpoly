#!/usr/bin/env python3
"""Analyze skew shadow logs from events.jsonl.

Reads skew_computed and skew_shadow_diff events and computes
validation metrics for shadow → live rollout decision.

Usage:
    python tools/analyze_skew.py [logfile]
    python tools/analyze_skew.py logs/events.jsonl
"""

from __future__ import annotations

import json
import sys
import math
from collections import Counter, defaultdict
from pathlib import Path


def load_events(path: str, event_type: str) -> list[dict]:
    """Load events of a specific type from JSONL file."""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("event") == event_type:
                events.append(data)
    return events


def stats(values: list[float]) -> dict:
    """Compute min/max/mean/std for a list of floats."""
    if not values:
        return {"min": 0, "max": 0, "mean": 0, "std": 0, "n": 0}
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n if n > 1 else 0.0
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(mean, 6),
        "std": round(math.sqrt(variance), 6),
        "n": n,
    }


def analyze_skew_computed(events: list[dict]):
    """Analyze skew_computed events."""
    if not events:
        print("  No skew_computed events found.")
        return

    print(f"  Total ticks: {len(events)}")
    print(f"  Shadow mode: {events[0].get('shadow', 'unknown')}")
    print()

    # Component distributions (UP token)
    components = {
        "velocity": [e.get("up_vel", 0) for e in events],
        "imbalance": [e.get("up_imb", 0) for e in events],
        "inventory": [e.get("up_inv", 0) for e in events],
        "underlying": [e.get("up_lead", 0) for e in events],
    }

    print("  Component Distribution (UP token):")
    print(f"  {'Component':<14} {'Min':>8} {'Max':>8} {'Mean':>8} {'Std':>8}")
    print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for name, vals in components.items():
        s = stats(vals)
        print(f"  {name:<14} {s['min']:>8.4f} {s['max']:>8.4f} {s['mean']:>8.4f} {s['std']:>8.4f}")
    print()

    # Score distributions
    raw_scores = [e.get("up_raw", 0) for e in events]
    smooth_scores = [e.get("up_smooth", 0) for e in events]
    print("  Score Distribution (UP token):")
    for label, vals in [("raw_score", raw_scores), ("smoothed", smooth_scores)]:
        s = stats(vals)
        print(f"  {label:<14} {s['min']:>8.4f} {s['max']:>8.4f} {s['mean']:>8.4f} {s['std']:>8.4f}")
    print()

    # Regime breakdown
    regimes_up = Counter(e.get("up_regime", "unknown") for e in events)
    regimes_dn = Counter(e.get("dn_regime", "unknown") for e in events)
    total = len(events)
    print("  Regime Breakdown:")
    print(f"  {'Regime':<18} {'UP %':>8} {'DN %':>8}")
    print(f"  {'-'*18} {'-'*8} {'-'*8}")
    all_regimes = sorted(set(list(regimes_up.keys()) + list(regimes_dn.keys())))
    for regime in all_regimes:
        up_pct = regimes_up.get(regime, 0) / total * 100
        dn_pct = regimes_dn.get(regime, 0) / total * 100
        print(f"  {regime:<18} {up_pct:>7.1f}% {dn_pct:>7.1f}%")
    print()

    # Adjustment frequency
    up_res = [abs(e.get("up_res_adj", 0)) for e in events]
    active = sum(1 for v in up_res if v > 0.0001)
    print(f"  Adjustment Activity: {active}/{total} ticks ({active/total*100:.1f}%) have |reservation_adj| > 0.01%")

    # Adjustment magnitudes
    print("\n  Adjustment Magnitudes (UP token):")
    for field, label in [("up_res_adj", "reservation"), ("up_bid_adj", "bid_adj"), ("up_ask_adj", "ask_adj")]:
        vals = [e.get(field, 0) for e in events]
        s = stats(vals)
        print(f"  {label:<14} {s['min']:>8.4f} {s['max']:>8.4f} {s['mean']:>8.4f} {s['std']:>8.4f}")


def analyze_shadow_diff(events: list[dict]):
    """Analyze skew_shadow_diff events."""
    if not events:
        print("  No skew_shadow_diff events found.")
        return

    print(f"  Total diff entries: {len(events)}")

    # Split by side
    by_side = defaultdict(list)
    for e in events:
        by_side[e.get("side", "?")].append(e)

    for side in sorted(by_side.keys()):
        side_events = by_side[side]
        bid_ticks = [e.get("diff_bid_ticks", 0) for e in side_events]
        ask_ticks = [e.get("diff_ask_ticks", 0) for e in side_events]

        print(f"\n  {side} Token ({len(side_events)} entries):")
        print(f"    diff_bid_ticks: {stats(bid_ticks)}")
        print(f"    diff_ask_ticks: {stats(ask_ticks)}")

        # Distribution of tick changes
        bid_dist = Counter(int(t) for t in bid_ticks)
        ask_dist = Counter(int(t) for t in ask_ticks)
        print(f"    Bid tick distribution: {dict(sorted(bid_dist.items()))}")
        print(f"    Ask tick distribution: {dict(sorted(ask_dist.items()))}")


def main():
    if len(sys.argv) < 2:
        logfile = "logs/events.jsonl"
    else:
        logfile = sys.argv[1]

    if not Path(logfile).exists():
        print(f"Error: {logfile} not found")
        sys.exit(1)

    print(f"Analyzing: {logfile}")
    print(f"{'=' * 60}")

    # Analyze skew_computed
    print("\n[1] SKEW COMPUTED")
    print("-" * 40)
    computed = load_events(logfile, "skew_computed")
    analyze_skew_computed(computed)

    # Analyze shadow diffs
    print(f"\n[2] SHADOW DIFF (Price Impact)")
    print("-" * 40)
    diffs = load_events(logfile, "skew_shadow_diff")
    analyze_shadow_diff(diffs)

    # Summary
    print(f"\n{'=' * 60}")
    print("ROLLOUT CHECKLIST:")
    if computed:
        active = sum(1 for e in computed if abs(e.get("up_res_adj", 0)) > 0.0001)
        total = len(computed)
        print(f"  [{'x' if total >= 100 else ' '}] 100+ skew_computed events (have {total})")
        print(f"  [{'x' if active/total < 0.9 else ' '}] Not 100% active (currently {active/total*100:.0f}%)")

        # Check bounds
        max_res = max(abs(e.get("up_res_adj", 0)) for e in computed)
        max_bid = max(abs(e.get("up_bid_adj", 0)) for e in computed)
        max_ask = max(abs(e.get("up_ask_adj", 0)) for e in computed)
        print(f"  [{'x' if max_res <= 0.011 else ' '}] reservation_adj within bounds (max={max_res:.4f})")
        print(f"  [{'x' if max_bid <= 0.006 else ' '}] bid_adj within bounds (max={max_bid:.4f})")
        print(f"  [{'x' if max_ask <= 0.006 else ' '}] ask_adj within bounds (max={max_ask:.4f})")
    else:
        print("  [ ] No data yet — run bot with shadow_mode=true first")

    print()


if __name__ == "__main__":
    main()
