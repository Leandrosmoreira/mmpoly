#!/usr/bin/env python3
"""CLI tool to scan and display active Polymarket crypto markets.

Usage:
    python tools/scan_markets.py                  # scan BTC 15m (default)
    python tools/scan_markets.py btc eth          # scan BTC + ETH
    python tools/scan_markets.py btc --interval 5m  # scan BTC 5m
    python tools/scan_markets.py --all            # scan all coins + intervals
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.market_scanner import (
    COINS, INTERVALS, discover_all_active, DiscoveredMarket,
)


def print_market(m: DiscoveredMarket, index: int):
    remaining = m.time_remaining
    status = "ACTIVE" if remaining > 60 else ("CLOSING" if remaining > 15 else "EXPIRED")

    print(f"\n  [{index}] {m.question}")
    print(f"      Name:         {m.name}")
    print(f"      Slug:         {m.slug}")
    print(f"      Status:       {status} ({remaining:.0f}s remaining)")
    print(f"      Condition ID: {m.condition_id}")
    print(f"      Token UP:     {m.token_up}")
    print(f"      Token DOWN:   {m.token_down}")
    print(f"      Liquidity:    ${m.liquidity:,.2f}")
    print(f"      Best Bid:     {m.best_bid}")
    print(f"      Best Ask:     {m.best_ask}")
    print(f"      Spread:       {m.spread}")
    print(f"      Orders:       {'Yes' if m.accepting_orders else 'No'}")


async def main():
    parser = argparse.ArgumentParser(description="Scan Polymarket crypto markets")
    parser.add_argument("coins", nargs="*", default=["btc"],
                       help="Coins to scan (e.g. btc eth sol)")
    parser.add_argument("--interval", "-i", default="15m",
                       choices=list(INTERVALS.keys()),
                       help="Interval (default: 15m)")
    parser.add_argument("--all", "-a", action="store_true",
                       help="Scan all coins and intervals")
    parser.add_argument("--yaml", "-y", action="store_true",
                       help="Output in YAML format for markets.yaml")

    args = parser.parse_args()

    coins = COINS if args.all else args.coins
    intervals = list(INTERVALS.keys()) if args.all else [args.interval]

    print(f"\nScanning Polymarket: coins={coins} intervals={intervals}\n")
    print("=" * 60)

    markets = await discover_all_active(coins, intervals)

    if not markets:
        print("\n  No active markets found.\n")
        print("  Possible reasons:")
        print("  - Between windows (wait a few seconds)")
        print("  - Markets not available for this coin/interval")
        print("  - API issue")
        return

    print(f"\n  Found {len(markets)} active market(s):")

    for i, m in enumerate(markets, 1):
        print_market(m, i)

    if args.yaml:
        print("\n\n# === YAML for config/markets.yaml ===")
        print("mode: manual\nmarkets:")
        for m in markets:
            print(f"  - name: \"{m.name}\"")
            print(f"    condition_id: \"{m.condition_id}\"")
            print(f"    token_up: \"{m.token_up}\"")
            print(f"    token_down: \"{m.token_down}\"")
            print(f"    end_ts: {m.end_ts}")
            print(f"    enabled: true")
            print()

    print()


if __name__ == "__main__":
    asyncio.run(main())
