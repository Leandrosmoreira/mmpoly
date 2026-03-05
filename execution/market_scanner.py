"""Market scanner — discovers and refreshes Polymarket crypto minute markets.

Uses Gamma API for discovery + CLOB API for trading data.
Supports BTC, ETH, SOL, etc. in 1m, 5m, 15m windows.
"""

from __future__ import annotations

import time
import asyncio
import aiohttp
import structlog
from dataclasses import dataclass
from typing import Optional

logger = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Supported coins and intervals
COINS = ["btc", "eth", "sol", "xrp", "doge"]
INTERVALS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
}


@dataclass
class DiscoveredMarket:
    """A market discovered from Polymarket API."""
    name: str
    slug: str
    condition_id: str
    question_id: str
    token_up: str       # YES / Up token
    token_down: str     # NO / Down token
    end_ts: float       # when this window closes
    start_ts: float     # when this window opened
    question: str
    active: bool
    accepting_orders: bool
    min_order_size: float
    tick_size: float
    liquidity: float
    best_bid: float
    best_ask: float
    spread: float


def _build_slug(coin: str, interval: str, ts: Optional[int] = None) -> str:
    """Build market slug for a given coin, interval, and timestamp.

    Slug format: {coin}-updown-{interval}-{rounded_timestamp}
    """
    interval_seconds = INTERVALS.get(interval, 900)
    if ts is None:
        ts = int(time.time() // interval_seconds) * interval_seconds
    return f"{coin}-updown-{interval}-{ts}"


def _get_current_window_ts(interval: str) -> int:
    """Get the rounded timestamp for the current window."""
    secs = INTERVALS.get(interval, 900)
    return int(time.time() // secs) * secs


def _get_next_window_ts(interval: str) -> int:
    """Get the rounded timestamp for the next window."""
    secs = INTERVALS.get(interval, 900)
    return int(time.time() // secs) * secs + secs


async def discover_market(
    session: aiohttp.ClientSession,
    coin: str,
    interval: str,
    window_ts: Optional[int] = None,
) -> Optional[DiscoveredMarket]:
    """Discover a single market by coin + interval + window.

    Queries Gamma API by slug, returns market data if found and active.
    """
    slug = _build_slug(coin, interval, window_ts)

    try:
        async with session.get(
            f"{GAMMA_API}/markets",
            params={
                "slug": slug,
                "active": "true",
                "closed": "false",
                "limit": 1,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.debug("gamma_api_error", status=resp.status, slug=slug)
                return None

            markets = await resp.json()

            if not markets:
                logger.debug("market_not_found", slug=slug)
                return None

            m = markets[0]

            # Parse token IDs
            clob_tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])

            if len(clob_tokens) < 2 or len(outcomes) < 2:
                logger.warning("invalid_market_tokens", slug=slug)
                return None

            # Map outcomes to UP/DOWN
            # outcomes[0] = "Up", outcomes[1] = "Down" (standard for crypto markets)
            token_up = clob_tokens[0]
            token_down = clob_tokens[1]

            # Handle non-standard outcome ordering
            for i, outcome in enumerate(outcomes):
                if outcome.lower() in ("up", "yes"):
                    token_up = clob_tokens[i]
                elif outcome.lower() in ("down", "no"):
                    token_down = clob_tokens[i]

            # Parse timestamps
            end_date = m.get("endDate", m.get("end_date_iso", ""))
            start_date = m.get("eventStartTime", m.get("startDate", ""))
            end_ts = _parse_iso_ts(end_date)
            start_ts = _parse_iso_ts(start_date)

            return DiscoveredMarket(
                name=f"{coin}-{interval}",
                slug=slug,
                condition_id=m.get("conditionId", ""),
                question_id=m.get("questionID", ""),
                token_up=token_up,
                token_down=token_down,
                end_ts=end_ts,
                start_ts=start_ts,
                question=m.get("question", ""),
                active=m.get("active", False),
                accepting_orders=m.get("acceptingOrders", False),
                min_order_size=float(m.get("orderMinSize", 5)),
                tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
                liquidity=float(m.get("liquidity", 0)),
                best_bid=float(m.get("bestBid", 0)),
                best_ask=float(m.get("bestAsk", 0)),
                spread=float(m.get("spread", 0)),
            )

    except asyncio.TimeoutError:
        logger.warning("gamma_api_timeout", slug=slug)
        return None
    except Exception as e:
        logger.error("discover_market_error", slug=slug, error=str(e))
        return None


async def discover_all_active(
    coins: list[str] | None = None,
    intervals: list[str] | None = None,
) -> list[DiscoveredMarket]:
    """Discover all active markets for given coins and intervals.

    Checks current window and next window for each combination.
    """
    if coins is None:
        coins = ["btc"]
    if intervals is None:
        intervals = ["15m"]

    markets = []

    async with aiohttp.ClientSession() as session:
        tasks = []

        for coin in coins:
            for interval in intervals:
                # Current window
                current_ts = _get_current_window_ts(interval)
                tasks.append(discover_market(session, coin, interval, current_ts))

                # Next window (may already be accepting orders)
                next_ts = _get_next_window_ts(interval)
                tasks.append(discover_market(session, coin, interval, next_ts))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, DiscoveredMarket) and r.active:
                markets.append(r)

    # Deduplicate by condition_id
    seen = set()
    unique = []
    for m in markets:
        if m.condition_id not in seen:
            seen.add(m.condition_id)
            unique.append(m)

    return unique


async def scan_loop(
    coins: list[str],
    intervals: list[str],
    on_new_market,
    on_market_expired,
    scan_interval_s: float = 30.0,
):
    """Continuous scanner loop. Calls back when new markets appear or old ones expire.

    Args:
        coins: coins to scan (e.g. ["btc", "eth"])
        intervals: intervals to scan (e.g. ["15m"])
        on_new_market: async callback(DiscoveredMarket) when a new market is found
        on_market_expired: async callback(condition_id) when a market window expires
        scan_interval_s: how often to re-scan
    """
    active_markets: dict[str, DiscoveredMarket] = {}

    while True:
        try:
            discovered = await discover_all_active(coins, intervals)

            # Check for new markets
            for m in discovered:
                if m.condition_id not in active_markets:
                    if m.accepting_orders and m.time_remaining > 30:
                        active_markets[m.condition_id] = m
                        logger.info("new_market_found",
                                   name=m.name, slug=m.slug,
                                   time_remaining=f"{m.time_remaining:.0f}s",
                                   liquidity=m.liquidity)
                        await on_new_market(m)

            # Check for expired markets
            now = time.time()
            expired = [
                cid for cid, m in active_markets.items()
                if now >= m.end_ts
            ]
            for cid in expired:
                m = active_markets.pop(cid)
                logger.info("market_expired", name=m.name, slug=m.slug)
                await on_market_expired(cid)

        except Exception as e:
            logger.error("scan_loop_error", error=str(e))

        await asyncio.sleep(scan_interval_s)


def _parse_iso_ts(iso_str: str) -> float:
    """Parse ISO timestamp to Unix timestamp."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime, timezone
        # Handle various ISO formats
        iso_str = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        return 0.0


# Add time_remaining property dynamically
DiscoveredMarket.time_remaining = property(
    lambda self: max(0, self.end_ts - time.time())
)


# === CLI: run standalone to discover markets ===
if __name__ == "__main__":
    import sys

    coins = sys.argv[1:] if len(sys.argv) > 1 else ["btc"]
    intervals = ["15m"]

    print(f"\nScanning Polymarket for: {coins} x {intervals}\n")

    async def _main():
        markets = await discover_all_active(coins, intervals)

        if not markets:
            print("No active markets found.\n")
            print("This could mean:")
            print("  - The current 15-min window just expired")
            print("  - Markets haven't opened for the next window yet")
            print("  - The coin/interval combo doesn't exist on Polymarket")
            return

        for m in markets:
            remaining = m.time_remaining
            print(f"{'='*60}")
            print(f"  Market:       {m.question}")
            print(f"  Name:         {m.name}")
            print(f"  Slug:         {m.slug}")
            print(f"  Condition ID: {m.condition_id}")
            print(f"  Token UP:     {m.token_up}")
            print(f"  Token DOWN:   {m.token_down}")
            print(f"  End:          {remaining:.0f}s remaining")
            print(f"  Liquidity:    ${m.liquidity:,.2f}")
            print(f"  Best Bid:     {m.best_bid}")
            print(f"  Best Ask:     {m.best_ask}")
            print(f"  Spread:       {m.spread}")
            print(f"  Accepting:    {m.accepting_orders}")
            print()

        # Output YAML format for easy copy-paste
        print("\n# === Copy to config/markets.yaml ===\nmarkets:")
        for m in markets:
            print(f"  - name: \"{m.name}\"")
            print(f"    condition_id: \"{m.condition_id}\"")
            print(f"    token_up: \"{m.token_up}\"")
            print(f"    token_down: \"{m.token_down}\"")
            print(f"    end_ts: {m.end_ts}")
            print(f"    enabled: true")
            print()

    asyncio.run(_main())
