"""WebSocket feed handler for Polymarket CLOB.

Handles book updates and trade events.
Supports dynamic subscription for new tokens at runtime.
Auto-reconnects on disconnect with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import structlog
import aiohttp
from typing import Callable, Optional

logger = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WSFeed:
    """WebSocket feed for live book updates."""

    def __init__(
        self,
        token_ids: list[str],
        on_book_update: Callable[[str, list, list], None],
        on_trade: Optional[Callable] = None,
    ):
        self._initial_tokens = list(token_ids)
        self._subscribed_tokens: set[str] = set()
        self.on_book_update = on_book_update
        self.on_trade = on_trade
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._pending_subs: list[str] = []

    async def start(self):
        """Start WebSocket connection with auto-reconnect."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ws_error", error=str(e))

            if self._running:
                logger.info("ws_reconnecting", delay=self._reconnect_delay)
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

    async def subscribe(self, token_ids: list[str]):
        """Subscribe to new tokens at runtime.

        If WS is connected, subscribes immediately.
        Otherwise queues for next reconnect.
        """
        new_tokens = [t for t in token_ids if t and t not in self._subscribed_tokens]
        if not new_tokens:
            return

        if self.is_connected:
            for token_id in new_tokens:
                try:
                    await self._ws.send_json({
                        "type": "subscribe",
                        "channel": "book",
                        "assets_ids": [token_id],
                    })
                    self._subscribed_tokens.add(token_id)
                    logger.info("ws_subscribed_runtime", token_id=token_id[:16] + "...")
                except Exception as e:
                    logger.error("ws_subscribe_error", token_id=token_id[:16], error=str(e))
                    self._pending_subs.append(token_id)
        else:
            self._pending_subs.extend(new_tokens)

    async def _connect_and_listen(self):
        """Connect to WS and process messages."""
        self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(
                WS_URL,
                heartbeat=30,
                receive_timeout=60,
            )
            logger.info("ws_connected", url=WS_URL)
            self._reconnect_delay = 1.0

            # Subscribe to all known tokens
            all_tokens = set(self._initial_tokens) | set(self._pending_subs) | self._subscribed_tokens
            self._pending_subs.clear()

            for token_id in all_tokens:
                if not token_id:
                    continue
                await self._ws.send_json({
                    "type": "subscribe",
                    "channel": "book",
                    "assets_ids": [token_id],
                })
                self._subscribed_tokens.add(token_id)

            logger.info("ws_subscribed_all", count=len(all_tokens))

            # Process messages
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("ws_msg_error", error=str(self._ws.exception()))
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("ws_connection_error", error=str(e))
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    def _handle_message(self, raw: str):
        """Parse and route WS message. Sync — no I/O.

        Polymarket WS may send:
        - A single dict: {"type": "book", ...}
        - An array of dicts: [{"type": "book", ...}, ...]
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Handle array messages: iterate each element (flatten nested arrays)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._process_event(item)
                elif isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, dict):
                            self._process_event(sub)
        elif isinstance(data, dict):
            self._process_event(data)

    def _process_event(self, data):
        """Process a single WS event dict."""
        if not isinstance(data, dict):
            return
        msg_type = data.get("type", data.get("channel", ""))

        if msg_type == "book":
            asset_id = data.get("asset_id", "")
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if asset_id and (bids or asks):
                self.on_book_update(asset_id, bids, asks)

        elif msg_type == "price_change":
            changes = data.get("changes", [data])
            for change in changes:
                if not isinstance(change, dict):
                    continue
                asset_id = change.get("asset_id", "")
                bids = change.get("bids", [])
                asks = change.get("asks", [])
                if asset_id and (bids or asks):
                    self.on_book_update(asset_id, bids, asks)

        elif msg_type in ("trade", "last_trade_price"):
            if self.on_trade:
                self.on_trade(data)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed
