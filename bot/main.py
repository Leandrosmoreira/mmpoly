"""GabaBook MM Bot — Main entry point.

Supports two market modes:
- auto: discovers markets via Gamma API scanner (recommended)
- manual: uses static markets from config/markets.yaml
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import yaml
import structlog
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from bot.logger import setup_logging, log_snapshot
from core.types import BotConfig, BotState, Direction, Fill, GridConfig, IntentType, MarketState, Side
from core.engine import Engine
from data.book import BookCache
from data.inventory import InventoryTracker
from data.fills import FillsCache
from execution.poly_client import PolyClient
from execution.order_manager import OrderManager
from execution.ws_feed import WSFeed
from execution.market_scanner import DiscoveredMarket, discover_all_active
from risk.manager import RiskManager

logger = structlog.get_logger()


class GabaBot:
    """Main bot orchestrator."""

    def __init__(self, config_path: str = "config/bot.yaml", markets_path: str = "config/markets.yaml"):
        self.cfg = self._load_config(config_path)
        self._markets_raw = self._load_markets_yaml(markets_path)

        # Scanner config
        self._market_mode = self._markets_raw.get("mode", "auto")
        scanner_cfg = self._markets_raw.get("scanner", {})
        self._scan_coins = scanner_cfg.get("coins", ["btc"])
        self._scan_intervals = scanner_cfg.get("intervals", ["15m"])
        self._scan_interval_s = scanner_cfg.get("scan_interval_s", 30)
        self._min_liquidity = scanner_cfg.get("min_liquidity", 1000)
        self._min_time_remaining = scanner_cfg.get("min_time_remaining", 60)

        # Setup logging
        setup_logging(self.cfg.log_dir)

        # Components
        self.book_cache = BookCache()
        self.inventory = InventoryTracker()
        self.fills = FillsCache()
        self.order_mgr = OrderManager(self.cfg)
        self.risk_mgr = RiskManager(self.cfg)
        self.poly_client = PolyClient(self.cfg)

        # Per-market state
        self.engines: dict[str, Engine] = {}
        self.markets: dict[str, MarketState] = {}
        self._token_to_market: dict[str, str] = {}
        self._condition_to_name: dict[str, str] = {}

        self.ws_feed: WSFeed | None = None
        self._running = False
        self._last_snapshot_ts = 0.0

        # Manual mode: load static markets
        if self._market_mode == "manual":
            static_markets = self._markets_raw.get("markets", [])
            if isinstance(static_markets, list):
                for mcfg in static_markets:
                    if mcfg.get("enabled", False):
                        self._add_market_from_config(mcfg)

    # === Config loading ===

    def _load_config(self, path: str) -> BotConfig:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = BotConfig()
            for k, v in data.items():
                if k == "grid" and isinstance(v, dict):
                    # Converte dict YAML para GridConfig dataclass
                    grid_cfg = GridConfig()
                    for gk, gv in v.items():
                        if hasattr(grid_cfg, gk):
                            setattr(grid_cfg, gk, gv)
                    cfg.grid = grid_cfg
                elif hasattr(cfg, k):
                    setattr(cfg, k, v)

            # Atalho: grid_levels sobrescreve grid.mid_*_levels e max_levels
            if cfg.grid_levels > 0:
                n = cfg.grid_levels
                cfg.grid.max_levels = n
                cfg.grid.mid_buy_levels = n
                cfg.grid.mid_sell_levels = n
                cfg.max_orders_per_side = max(4, n * 2)
                cfg.max_position = n * cfg.grid.level_size * 2
                cfg.net_hard_limit = n * cfg.grid.level_size * 2.5
                cfg.net_soft_limit = n * cfg.grid.level_size

            return cfg
        except FileNotFoundError:
            logger.warning("config_not_found", path=path, msg="using defaults")
            return BotConfig()

    def _load_markets_yaml(self, path: str) -> dict:
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("markets_config_not_found", path=path)
            return {}

    # === Market management ===

    def _add_market_from_config(self, mcfg: dict):
        name = mcfg["name"]
        if name in self.markets:
            return
        market = MarketState(
            name=name,
            condition_id=mcfg["condition_id"],
            token_up=mcfg["token_up"],
            token_down=mcfg["token_down"],
            end_ts=mcfg.get("end_ts", time.time() + 600),
        )
        self._register_market(market)

    def _add_market_from_discovery(self, discovered: DiscoveredMarket):
        if discovered.condition_id in self._condition_to_name:
            return
        if discovered.liquidity < self._min_liquidity:
            return
        if discovered.time_remaining < self._min_time_remaining:
            return

        name = f"{discovered.name}-{discovered.slug.split('-')[-1]}"
        market = MarketState(
            name=name,
            condition_id=discovered.condition_id,
            token_up=discovered.token_up,
            token_down=discovered.token_down,
            end_ts=discovered.end_ts,
        )
        self._register_market(market)
        logger.info("market_added_auto",
                    name=name, question=discovered.question,
                    time_remaining=f"{discovered.time_remaining:.0f}s",
                    liquidity=f"${discovered.liquidity:,.0f}",
                    spread=discovered.spread)

    def _register_market(self, market: MarketState):
        self.markets[market.name] = market
        self.engines[market.name] = Engine(market, self.cfg)
        self._token_to_market[market.token_up] = market.name
        self._token_to_market[market.token_down] = market.name
        self._condition_to_name[market.condition_id] = market.name
        logger.info("market_registered", name=market.name,
                    condition_id=market.condition_id[:16] + "...",
                    end_ts=market.end_ts)

    async def _remove_expired_markets(self):
        now = time.time()
        expired = [
            name for name, m in self.markets.items()
            if m.end_ts > 0 and now >= m.end_ts
        ]
        for name in expired:
            market = self.markets[name]

            # Cancel all orders
            cancel_intents = self.order_mgr.cancel_all_for_market(name)
            for ci in cancel_intents:
                if ci.order_id:
                    await self.poly_client.cancel_order(ci.order_id)
                    self.order_mgr.remove(ci.order_id)

            inv = self.inventory.get(name)
            logger.info("market_expired", name=name, net=inv.net,
                       realized_pnl=inv.realized_pnl)

            self._token_to_market.pop(market.token_up, None)
            self._token_to_market.pop(market.token_down, None)
            self._condition_to_name.pop(market.condition_id, None)
            self.engines.pop(name, None)
            self.markets.pop(name, None)

    # === WS callback ===

    def _on_book_update(self, token_id: str, bids: list, asks: list):
        self.book_cache.update(token_id, bids, asks)
        market_name = self._token_to_market.get(token_id)
        if market_name and market_name in self.markets:
            market = self.markets[market_name]
            book = self.book_cache.get(token_id)
            if book:
                if token_id == market.token_up:
                    market.book_up = book
                else:
                    market.book_down = book

    # === Main run ===

    async def run(self):
        logger.info("bot_starting", dry_run=self.cfg.dry_run,
                    mode=self._market_mode, coins=self._scan_coins,
                    intervals=self._scan_intervals)

        self.poly_client.connect()
        self._running = True

        if self._market_mode == "auto":
            await self._discover_initial_markets()

        if not self.markets:
            logger.warning("no_markets_found", msg="Waiting for scanner...")

        # Create WS feed
        all_tokens = []
        for m in self.markets.values():
            all_tokens.extend([m.token_up, m.token_down])
        self.ws_feed = WSFeed(
            token_ids=all_tokens,
            on_book_update=self._on_book_update,
        )

        # Warmup books via REST
        await self._warmup_books()

        # Run all loops concurrently
        try:
            tasks = [self._main_loop(), self.ws_feed.start()]
            if self._market_mode == "auto":
                tasks.append(self._scanner_loop())
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("bot_cancelled")
        finally:
            await self.shutdown()

    async def _discover_initial_markets(self):
        logger.info("discovering_markets",
                    coins=self._scan_coins, intervals=self._scan_intervals)
        discovered = await discover_all_active(self._scan_coins, self._scan_intervals)
        for m in discovered:
            self._add_market_from_discovery(m)
        logger.info("initial_discovery_complete", markets_found=len(self.markets))

    async def _scanner_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self._scan_interval_s)
                await self._remove_expired_markets()

                discovered = await discover_all_active(
                    self._scan_coins, self._scan_intervals
                )
                new_count = 0
                for m in discovered:
                    if m.condition_id not in self._condition_to_name:
                        self._add_market_from_discovery(m)
                        new_count += 1

                        # Subscribe new tokens to WS dynamically
                        if self.ws_feed:
                            await self.ws_feed.subscribe([m.token_up, m.token_down])

                        # Warmup book for new market
                        market_name = self._condition_to_name.get(m.condition_id)
                        if market_name and market_name in self.markets:
                            await self._warmup_market(self.markets[market_name])

                if new_count > 0:
                    logger.info("scanner_found_new", count=new_count,
                               total_active=len(self.markets))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("scanner_error", error=str(e))

    async def _warmup_books(self):
        for market in self.markets.values():
            await self._warmup_market(market)

    async def _warmup_market(self, market: MarketState):
        for token_id in [market.token_up, market.token_down]:
            data = await self.poly_client.get_order_book_async(token_id)
            if data:
                self.book_cache.update_from_snapshot(token_id, data)
                book = self.book_cache.get(token_id)
                if book:
                    if token_id == market.token_up:
                        market.book_up = book
                    else:
                        market.book_down = book
                logger.info("book_warmup", market=market.name,
                           side="UP" if token_id == market.token_up else "DOWN",
                           bid=book.best_bid if book else 0,
                           ask=book.best_ask if book else 0)

    # === Main decision loop ===

    async def _main_loop(self):
        await asyncio.sleep(2.0)  # Wait for WS + books

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("tick_error", error=str(e), exc_info=True)
                await asyncio.sleep(1.0)

    async def _tick(self):
        now = time.time()

        # Expire old orders first
        expired_intents = self.order_mgr.get_expired_orders()
        if expired_intents:
            expired_intents = self.risk_mgr.filter_intents(expired_intents)
            await self._execute_intents(expired_intents)

        # Tick each engine
        for name in list(self.engines.keys()):
            if name not in self.markets:
                continue
            engine = self.engines[name]
            market = self.markets[name]
            market.inventory = self.inventory.get(name)

            # Pass live order IDs + order_mgr para cancel seletivo por nivel
            live_ids = self.order_mgr.get_order_ids_for_market(name)
            intents = engine.tick(live_ids, self.order_mgr)

            if intents:
                intents = self.risk_mgr.filter_intents(intents)
                await self._execute_intents(intents)

        # Periodic snapshot
        if now - self._last_snapshot_ts > self.cfg.snapshot_interval_s:
            self._log_snapshots()
            self._last_snapshot_ts = now

    # === Intent execution ===

    async def _execute_intents(self, intents: list):
        for intent in intents:
            if intent.type == IntentType.PLACE_ORDER:
                market = self.markets.get(intent.market_name)
                if not market:
                    continue
                token_id = (market.token_up if intent.side == Side.UP
                           else market.token_down)

                live_order = await self.poly_client.place_order(intent, token_id)
                if live_order:
                    live_order.level = intent.level  # propaga nivel do grid
                    self.order_mgr.register(live_order)

            elif intent.type == IntentType.CANCEL_ORDER:
                if intent.order_id:
                    success = await self.poly_client.cancel_order(intent.order_id)
                    if success:
                        self.order_mgr.remove(intent.order_id)
                        self.risk_mgr.record_cancel()

            elif intent.type == IntentType.CANCEL_ALL:
                if intent.market_name == "ALL":
                    await self.poly_client.cancel_all()
                    for ci in self.order_mgr.cancel_all():
                        self.order_mgr.remove(ci.order_id)
                else:
                    for ci in self.order_mgr.cancel_all_for_market(intent.market_name):
                        if ci.order_id:
                            await self.poly_client.cancel_order(ci.order_id)
                            self.order_mgr.remove(ci.order_id)

            elif intent.type == IntentType.KILL_SWITCH:
                logger.critical("kill_switch_triggered", reason=intent.reason)
                await self.poly_client.cancel_all()
                for ci in self.order_mgr.cancel_all():
                    self.order_mgr.remove(ci.order_id)
                self._running = False

    # === Fill handling ===

    def handle_fill(self, fill: Fill):
        """Process a fill event. Called from WS trade handler or polling."""
        # Update inventory
        self.inventory.apply_fill(fill)
        self.fills.add(fill)

        # Cancel-on-fill
        cancel_intents = self.order_mgr.on_fill(fill)
        if cancel_intents:
            # Schedule async cancels
            asyncio.create_task(self._execute_intents(cancel_intents))

        # Request requote on the engine for this market
        engine = self.engines.get(fill.market_name)
        if engine:
            engine.request_requote()

        # Track PnL in risk manager
        inv = self.inventory.get(fill.market_name)
        self.risk_mgr.record_fill_pnl(inv.realized_pnl)

        logger.info("fill", event="fill", market=fill.market_name,
                    side=fill.side.value, direction=fill.direction.value,
                    px=fill.price, sz=fill.size, is_maker=fill.is_maker,
                    net=inv.net, realized_pnl=inv.realized_pnl)

    # === Snapshots ===

    def _log_snapshots(self):
        risk_status = self.risk_mgr.status()
        for name in self.markets:
            inv = self.inventory.get(name)
            log_snapshot(name, inv, risk_status)

    # === Shutdown ===

    async def shutdown(self):
        logger.info("bot_shutting_down")
        self._running = False

        await self.poly_client.cancel_all()

        if self.ws_feed:
            await self.ws_feed.stop()

        total_pnl = self.inventory.total_realized_pnl()
        logger.info("bot_shutdown_complete",
                    total_realized_pnl=total_pnl,
                    total_fills=self.fills.count())


def main():
    config_path = os.environ.get("BOT_CONFIG", "config/bot.yaml")
    markets_path = os.environ.get("MARKETS_CONFIG", "config/markets.yaml")

    bot = GabaBot(config_path, markets_path)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")


if __name__ == "__main__":
    main()
