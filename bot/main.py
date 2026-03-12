"""GabaBook MM Bot — Main entry point.

Supports two market modes:
- auto: discovers markets via Gamma API scanner (recommended)
- manual: uses static markets from config/markets.yaml
"""

from __future__ import annotations

import asyncio
from collections import deque
import os
import sys
import time
import uuid
import yaml
import structlog
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from project root
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_root, ".env"))
# Fallback: bookpoly .env (API credentials)
# Tenta relativo ao projeto (~/mmpoly/../bookpoly) e home dir (~/bookpoly)
for _bp in [
    os.path.join(_root, "..", "bookpoly", ".env"),
    os.path.join(os.path.expanduser("~"), "bookpoly", ".env"),
]:
    if os.path.isfile(_bp):
        load_dotenv(_bp)
        break

from bot.logger import setup_logging, log_snapshot
from core.types import (
    BotConfig, BotState, Direction, Fill, GridConfig, IntentType,
    MarketState, Side, SkewConfig, SkewResult, SkewTimeScaling, SkewWeights, SomaConfig,
)
from core.engine import Engine
from core.skew import SkewEngine
from data.book import BookCache
from data.inventory import InventoryTracker
from data.fills import FillsCache
from execution.poly_client import PolyClient
from execution.order_manager import OrderManager
from execution.ws_feed import WSFeed
from execution.binance_feed import BinanceFeed
from execution.market_scanner import DiscoveredMarket, discover_all_active
from risk.manager import RiskManager
from core.errors import ErrorCode

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
        self.inventory.load_snapshot()  # Restore inventory from crash
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
        self.binance_feed: BinanceFeed | None = None
        self._running = False
        self._last_snapshot_ts = 0.0
        self._last_book_refresh_ts = 0.0
        self._book_refresh_interval_s = 30.0

        # Skew engines: {token_id: SkewEngine}
        self._skew_engines: dict[str, SkewEngine] = {}

        # BUG-010 fix: track last known realized_pnl per market
        # to compute delta (not cumulative) for risk manager
        self._last_pnl: dict[str, float] = {}

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
                elif k == "soma" and isinstance(v, dict):
                    soma_cfg = SomaConfig()
                    for sk, sv in v.items():
                        if hasattr(soma_cfg, sk):
                            setattr(soma_cfg, sk, sv)
                    cfg.soma = soma_cfg
                elif k == "skew" and isinstance(v, dict):
                    skew_cfg = SkewConfig()
                    for sk, sv in v.items():
                        if sk == "weights" and isinstance(sv, dict):
                            w = SkewWeights()
                            for wk, wv in sv.items():
                                if hasattr(w, wk):
                                    setattr(w, wk, wv)
                            skew_cfg.weights = w
                        elif sk == "time_scaling" and isinstance(sv, dict):
                            ts = SkewTimeScaling()
                            for tk, tv in sv.items():
                                if hasattr(ts, tk):
                                    setattr(ts, tk, tv)
                            skew_cfg.time_scaling = ts
                        elif hasattr(skew_cfg, sk):
                            setattr(skew_cfg, sk, sv)
                    cfg.skew = skew_cfg
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
        # BUG-011: Removed liquidity filter. New 15-minute markets start with
        # zero liquidity on Gamma API because no one has posted orders yet.
        # We ARE the market maker — can't wait for liquidity to bootstrap.
        # The old filter (liquidity < 1000) caused a chicken-and-egg problem:
        # bot won't add market → market has no liquidity → bot won't add market.
        if discovered.time_remaining < self._min_time_remaining:
            logger.info("market_rejected_time",
                        slug=discovered.slug,
                        time_remaining=f"{discovered.time_remaining:.0f}s",
                        min_required=self._min_time_remaining)
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

        # Create skew engines per token (UP and DOWN get separate engines)
        if self.cfg.skew.enabled:
            self._skew_engines[market.token_up] = SkewEngine(self.cfg.skew)
            self._skew_engines[market.token_down] = SkewEngine(self.cfg.skew)

        logger.info("market_registered", name=market.name,
                    condition_id=market.condition_id[:16] + "...",
                    end_ts=market.end_ts,
                    skew_enabled=self.cfg.skew.enabled)

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

            # BUG-011: unsubscribe WS tokens to prevent stale subscription leak
            if self.ws_feed:
                await self.ws_feed.unsubscribe([market.token_up, market.token_down])

            self._token_to_market.pop(market.token_up, None)
            self._token_to_market.pop(market.token_down, None)
            self._condition_to_name.pop(market.condition_id, None)
            self._skew_engines.pop(market.token_up, None)
            self._skew_engines.pop(market.token_down, None)
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

                # Feed skew engine with mid-price and imbalance data
                skew_eng = self._skew_engines.get(token_id)
                if skew_eng and book.is_valid:
                    skew_eng.update_mid(book.ts, book.mid)
                    skew_eng.update_imbalance(
                        book.ts, book.best_bid_sz, book.best_ask_sz,
                    )

    def _on_btc_price(self, ts: float, price: float):
        """Callback from BinanceFeed — propagate BTC price to all skew engines."""
        for eng in self._skew_engines.values():
            eng.update_underlying(ts, price)

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

        # Create Binance BTC feed for skew underlying_lead component
        if self.cfg.skew.enabled:
            self.binance_feed = BinanceFeed(
                on_price=self._on_btc_price,
            )

        # Run all loops concurrently
        # BUG-011: use return_exceptions=True so one crashed task
        # doesn't cancel all others (e.g. WS crash killing scanner)
        try:
            tasks = [self._main_loop(), self.ws_feed.start()]
            if self._market_mode == "auto":
                tasks.append(self._scanner_loop())
            if self.binance_feed:
                tasks.append(self.binance_feed.start())
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                    logger.critical("task_crashed", error=str(r), error_type=type(r).__name__)
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
                # BUG-011: scan faster when no markets are active (5s vs 30s)
                # so the bot picks up new markets quickly after transitions
                sleep_time = 5.0 if not self.markets else self._scan_interval_s
                await asyncio.sleep(sleep_time)
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
                logger.error("scanner_error", error=str(e),
                             error_code=ErrorCode.SCANNER_ERROR)

    async def _warmup_books(self):
        for market in self.markets.values():
            await self._warmup_market(market)

    async def _warmup_market(self, market: MarketState, silent: bool = False):
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
                if not silent:
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
                logger.error("tick_error", error=str(e), exc_info=True,
                             error_code=ErrorCode.TICK_ERROR)
                await asyncio.sleep(1.0)

    async def _tick(self):
        cycle_id = uuid.uuid4().hex[:8]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(cycle_id=cycle_id)

        now = time.time()

        # BUG-011: warn when no active markets (waiting for scanner)
        if not self.markets:
            logger.warning("no_active_markets",
                           msg="Waiting for scanner to discover markets")
            return

        # Periodic REST book refresh — fallback for quiet WS
        if now - self._last_book_refresh_ts > self._book_refresh_interval_s:
            for market in self.markets.values():
                await self._warmup_market(market, silent=True)
            self._last_book_refresh_ts = now

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

            # Compute skew before tick (feeds into quoter via engine)
            if self.cfg.skew.enabled:
                self._compute_skew(engine, market)

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

    # === Skew computation ===

    def _compute_skew(self, engine: Engine, market: MarketState):
        """Compute directional skew for both tokens and set on engine.

        In shadow_mode: computes and logs but passes None to engine,
        so quoter uses empty SkewResult (zero adjustments).
        In live mode: sets skew_up/skew_down on engine for quoter use.
        """
        inv = market.inventory
        t_remain = market.time_remaining_s

        skew_up_eng = self._skew_engines.get(market.token_up)
        skew_dn_eng = self._skew_engines.get(market.token_down)

        if not skew_up_eng or not skew_dn_eng:
            return

        spread_up = market.book_up.spread if market.book_up.is_valid else 0.10
        spread_dn = market.book_down.spread if market.book_down.is_valid else 0.10

        result_up = skew_up_eng.compute(
            net=inv.net, soft_limit=self.cfg.net_soft_limit,
            t_remain=t_remain, spread=spread_up,
        )
        result_dn = skew_dn_eng.compute(
            net=inv.net, soft_limit=self.cfg.net_soft_limit,
            t_remain=t_remain, spread=spread_dn,
        )

        # Log skew computation (always, even in shadow mode)
        logger.info("skew_computed",
                    market=market.name,
                    shadow=self.cfg.skew.shadow_mode,
                    # UP token
                    up_raw=round(result_up.raw_score, 4),
                    up_smooth=round(result_up.smoothed_score, 4),
                    up_regime=result_up.regime,
                    up_res_adj=round(result_up.reservation_adj, 4),
                    up_bid_adj=round(result_up.bid_adj, 4),
                    up_ask_adj=round(result_up.ask_adj, 4),
                    up_vel=round(result_up.components.velocity, 4),
                    up_imb=round(result_up.components.imbalance, 4),
                    up_inv=round(result_up.components.inventory, 4),
                    up_lead=round(result_up.components.underlying_lead, 4),
                    # DOWN token
                    dn_raw=round(result_dn.raw_score, 4),
                    dn_smooth=round(result_dn.smoothed_score, 4),
                    dn_regime=result_dn.regime,
                    dn_res_adj=round(result_dn.reservation_adj, 4),
                    dn_bid_adj=round(result_dn.bid_adj, 4),
                    dn_ask_adj=round(result_dn.ask_adj, 4),
                    # Context
                    net=inv.net, t_remain=round(t_remain, 0))

        # Shadow diff: log hypothetical price impact in ticks
        tick = self.cfg.tick
        for side_label, book, result in [
            ("UP", market.book_up, result_up),
            ("DOWN", market.book_down, result_dn),
        ]:
            if book.is_valid:
                old_bid = book.best_bid + tick
                new_bid = old_bid + result.reservation_adj + result.bid_adj
                old_ask = book.best_ask - tick
                new_ask = old_ask + result.reservation_adj + result.ask_adj
                logger.info("skew_shadow_diff",
                            market=market.name, side=side_label,
                            old_bid=round(old_bid, 2),
                            new_bid=round(new_bid, 2),
                            old_ask=round(old_ask, 2),
                            new_ask=round(new_ask, 2),
                            diff_bid_ticks=round((new_bid - old_bid) / tick),
                            diff_ask_ticks=round((new_ask - old_ask) / tick))

        if self.cfg.skew.shadow_mode:
            # Shadow: compute only, don't affect quotes
            engine.skew_up = None
            engine.skew_down = None
        else:
            # Live: apply skew adjustments to quoter
            engine.skew_up = result_up
            engine.skew_down = result_dn

    # === Intent execution ===

    async def _execute_intents(self, intents: list):
        # Track fills per market in this batch to prevent phantom fills.
        # When BUY UP fills, cancel-on-fill targets BUY DOWN. If both are
        # in the same expired batch, both could return "matched" — but only
        # one side actually filled. Allow max 1 fill per market per batch.
        #
        # BUG-009 fix: use a queue so cancel-on-fill intents are processed
        # INLINE (same fills_this_batch), not via asyncio.create_task which
        # would create a separate batch allowing phantom double fills.
        fills_this_batch: set[str] = set()
        queue: deque = deque(intents)

        while queue:
            intent = queue.popleft()

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
                elif (intent.direction == Direction.SELL
                      and self.poly_client._last_place_error == "no_balance"):
                    # Exchange says we don't have these shares — phantom inventory.
                    # Zero out this side to stop infinite SELL retry spam.
                    self.inventory.zero_side(intent.market_name, intent.side)

            elif intent.type == IntentType.CANCEL_ORDER:
                if intent.order_id:
                    # Lookup order BEFORE removing (need details for fill inference)
                    order = self.order_mgr.get(intent.order_id)
                    status = await self.poly_client.cancel_order(intent.order_id)
                    # Always remove from manager to prevent infinite cancel loops
                    self.order_mgr.remove(intent.order_id)
                    if status == "canceled":
                        self.risk_mgr.record_cancel()
                    elif status == "matched" and order:
                        market_key = order.market_name
                        if market_key in fills_this_batch:
                            # Already detected a fill in this market during
                            # this batch — skip to prevent phantom double fill.
                            # The real fill already triggered cancel-on-fill.
                            logger.warning("phantom_fill_blocked",
                                          order_id=order.order_id,
                                          market=order.market_name,
                                          side=order.side.value,
                                          direction=order.direction.value,
                                          error_code=ErrorCode.PHANTOM_FILL_BLOCKED)
                        else:
                            fills_this_batch.add(market_key)
                            fill = Fill(
                                order_id=order.order_id,
                                market_name=order.market_name,
                                token_id=order.token_id,
                                side=order.side,
                                direction=order.direction,
                                price=order.price,
                                size=order.remaining,
                                ts=time.time(),
                                is_maker=True,
                            )
                            # BUG-009: process cancel-on-fill INLINE
                            # (extends queue with same fills_this_batch)
                            cancel_on_fill = self.handle_fill(fill)
                            queue.extend(cancel_on_fill)

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

    def handle_fill(self, fill: Fill) -> list:
        """Process a fill event. Returns cancel-on-fill intents for inline execution.

        BUG-009: Previously used asyncio.create_task to execute cancel-on-fill,
        which created a separate _execute_intents call with its own fills_this_batch.
        This allowed the same order to be processed as "matched" twice (once blocked
        as phantom in the parent batch, once accepted in the child task's fresh batch),
        causing phantom inventory → losses → kill switch.

        Now returns cancel intents so the caller can process them in the same batch,
        sharing the same fills_this_batch set.
        """
        # Update inventory
        self.inventory.apply_fill(fill)
        self.fills.add(fill)

        # Cancel-on-fill: get intents but DON'T execute here
        cancel_intents = self.order_mgr.on_fill(fill) or []

        # Request requote on the engine for this market
        engine = self.engines.get(fill.market_name)
        if engine:
            engine.request_requote()

        # Track PnL in risk manager — BUG-010 fix: use DELTA pnl, not cumulative.
        # Previously passed inv.realized_pnl (cumulative) which caused:
        # 1. daily_pnl to grow geometrically (added cumulative on every fill)
        # 2. consecutive_losses to trigger on BUYs (cumulative stays negative)
        inv = self.inventory.get(fill.market_name)
        prev_pnl = self._last_pnl.get(fill.market_name, 0.0)
        delta_pnl = inv.realized_pnl - prev_pnl
        self._last_pnl[fill.market_name] = inv.realized_pnl
        self.risk_mgr.record_fill_pnl(delta_pnl)

        logger.info("fill_detected", market=fill.market_name,
                    side=fill.side.value, direction=fill.direction.value,
                    px=fill.price, sz=fill.size, is_maker=fill.is_maker,
                    net=inv.net, realized_pnl=inv.realized_pnl,
                    delta_pnl=round(delta_pnl, 4))

        return cancel_intents

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
        if self.binance_feed:
            await self.binance_feed.stop()

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
