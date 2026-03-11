"""Binance WebSocket feed for BTC/USDT price data.

Provides real-time BTC price to SkewEngine.update_underlying().
Uses miniTicker stream (~1s frequency, small payload).

Auto-reconnects on disconnect with exponential backoff (same pattern as ws_feed.py).
No authentication required — public stream only.
"""

from __future__ import annotations

import asyncio
import json
import time
import structlog
import aiohttp
from typing import Callable, Optional

from core.errors import ErrorCode

logger = structlog.get_logger()

BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"


class BinanceFeed:
    """WebSocket feed for Binance BTC/USDT price data.

    Connects to miniTicker stream and calls on_price(ts, price)
    on each update. Used by SkewEngine for underlying_lead component.
    """

    def __init__(
        self,
        on_price: Callable[[float, float], None],
        symbol: str = "btcusdt",
    ):
        self._on_price = on_price
        self._symbol = symbol
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._last_price: float = 0.0
        self._msg_count: int = 0

    @property
    def url(self) -> str:
        return f"{BINANCE_WS_BASE}/{self._symbol}@miniTicker"

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def last_price(self) -> float:
        return self._last_price

    async def start(self):
        """Start WebSocket connection with auto-reconnect."""
        self._running = True
        logger.info("binance_feed_starting", symbol=self._symbol)

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("binance_ws_error", error=str(e),
                             error_code=ErrorCode.BINANCE_WS_ERROR)

            if self._running:
                logger.info("binance_ws_reconnecting",
                            delay=self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay,
                )

    async def stop(self):
        """Stop WebSocket connection."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("binance_feed_stopped",
                    symbol=self._symbol, msgs=self._msg_count)

    async def _connect_and_listen(self):
        """Connect to Binance WS and process messages."""
        self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=30,
                receive_timeout=60,
            )
            logger.info("binance_ws_connected", url=self.url)
            self._reconnect_delay = 1.0  # Reset backoff on success

            # Process messages
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("binance_ws_msg_error",
                                 error=str(self._ws.exception()),
                                 error_code=ErrorCode.BINANCE_WS_ERROR)
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.CLOSING):
                    break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("binance_ws_connection_error", error=str(e),
                         error_code=ErrorCode.BINANCE_WS_DISCONNECTED)
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    def _handle_message(self, raw: str):
        """Parse miniTicker message and invoke callback.

        Expected payload:
        {
            "e": "24hrMiniTicker",
            "E": 1672515782136,     // Event time (ms)
            "s": "BTCUSDT",
            "c": "94250.50",        // Close price
            ...
        }
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if not isinstance(data, dict):
            return

        # Extract close price
        price_str = data.get("c")
        if not price_str:
            return

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        if price <= 0:
            return

        # Use Binance event time if available, fallback to local time
        event_time_ms = data.get("E")
        if event_time_ms:
            try:
                ts = float(event_time_ms) / 1000.0
            except (ValueError, TypeError):
                ts = time.time()
        else:
            ts = time.time()

        self._last_price = price
        self._msg_count += 1
        self._on_price(ts, price)
