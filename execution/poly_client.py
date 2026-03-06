"""Polymarket CLOB API client wrapper.

Supports both EOA wallets and Magic Link (email) wallets.
Magic wallets use signature_type=2 (POLY_GNOSIS_SAFE) for signing.

All sync py-clob-client calls are wrapped with asyncio.to_thread()
to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
import structlog
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from core.types import BotConfig, Direction, Intent, LiveOrder, Side

logger = structlog.get_logger()

CLOB_URL = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

SIG_TYPE_EOA = 0
SIG_TYPE_POLY_PROXY = 1
SIG_TYPE_POLY_GNOSIS = 2  # Magic Link / Email


class PolyClient:
    """Wrapper around py-clob-client for order management.

    All blocking SDK calls run in a thread pool via asyncio.to_thread().
    In dry_run mode, generates mock order IDs for testing the full flow.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._client: Optional[ClobClient] = None
        self._funder: str = ""

    def connect(self):
        """Initialize CLOB client with credentials from env.
        Accepts POLY_* or POLYMARKET_* (e.g. bookpoly .env)."""
        def _get(key: str, poly: str, polymarket: str) -> str:
            return os.environ.get(poly, "").strip() or os.environ.get(polymarket, "").strip()
        private_key = _get("POLY_PRIVATE_KEY", "POLY_PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY")
        api_key = _get("POLY_API_KEY", "POLY_API_KEY", "POLYMARKET_API_KEY")
        api_secret = _get("POLY_API_SECRET", "POLY_API_SECRET", "POLYMARKET_API_SECRET")
        api_passphrase = _get("POLY_API_PASSPHRASE", "POLY_API_PASSPHRASE", "POLYMARKET_PASSPHRASE")
        wallet_type = (os.environ.get("POLY_WALLET_TYPE") or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "eoa")).lower().strip()
        if wallet_type == "1":
            wallet_type = "proxy"
        elif wallet_type == "2":
            wallet_type = "magic"
        elif wallet_type not in ("eoa", "proxy", "magic", "email", "magic2", "magiclink"):
            wallet_type = "eoa"
        self._funder = _get("POLY_FUNDER", "POLY_FUNDER", "POLYMARKET_FUNDER")

        if not private_key:
            logger.warning("no_private_key", msg="Running without credentials (dry-run only)")
            return

        if wallet_type in ("magic", "email", "magic2", "magiclink"):
            sig_type = SIG_TYPE_POLY_GNOSIS
            logger.info("wallet_type", type="Magic Link (email)", sig_type=sig_type)
        elif wallet_type == "proxy":
            sig_type = SIG_TYPE_POLY_PROXY
            logger.info("wallet_type", type="Poly Proxy", sig_type=sig_type)
        else:
            sig_type = SIG_TYPE_EOA
            logger.info("wallet_type", type="EOA", sig_type=sig_type)

        creds = None
        if api_key:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

        self._client = ClobClient(
            CLOB_URL,
            key=private_key,
            chain_id=CHAIN_ID,
            signature_type=sig_type,
            funder=self._funder if self._funder else None,
            creds=creds,
        )

        if not api_key:
            try:
                creds = self._client.derive_api_key()
                self._client.set_api_creds(creds)
                logger.info("api_creds_derived",
                           api_key=(creds.api_key[:8] + "...") if creds and creds.api_key else "")
            except Exception as e:
                logger.error("derive_api_key_failed", error=str(e))
                raise

        try:
            self._client.get_ok()
            logger.info("poly_client_connected", wallet_type=wallet_type)
        except Exception as e:
            logger.error("connection_check_failed", error=str(e))

    async def place_order(self, intent: Intent, token_id: str) -> Optional[LiveOrder]:
        """Place a POST_ONLY limit order. Non-blocking."""
        now = time.time()

        if self.cfg.dry_run:
            mock_id = f"dry_{uuid.uuid4().hex[:12]}"
            logger.info("dry_run_order", order_id=mock_id,
                       market=intent.market_name,
                       side=intent.side, direction=intent.direction,
                       px=intent.price, sz=intent.size, reason=intent.reason)
            return LiveOrder(
                order_id=mock_id,
                market_name=intent.market_name,
                token_id=token_id,
                side=intent.side or Side.UP,
                direction=intent.direction or Direction.BUY,
                price=intent.price,
                size=intent.size,
                placed_at=now,
                ttl_ms=self.cfg.quote_ttl_ms,
            )

        if self._client is None:
            return None

        try:
            poly_side = BUY if intent.direction == Direction.BUY else SELL

            order_args = OrderArgs(
                price=intent.price,
                size=intent.size,
                side=poly_side,
                token_id=token_id,
            )

            signed_order = await asyncio.to_thread(
                self._client.create_order, order_args
            )
            resp = await asyncio.to_thread(
                self._client.post_order, signed_order, OrderType.GTC
            )

            if resp and resp.get("success"):
                order_id = resp.get("orderID", resp.get("order_id", ""))
                live = LiveOrder(
                    order_id=order_id,
                    market_name=intent.market_name,
                    token_id=token_id,
                    side=intent.side or Side.UP,
                    direction=intent.direction or Direction.BUY,
                    price=intent.price,
                    size=intent.size,
                    placed_at=now,
                    ttl_ms=self.cfg.quote_ttl_ms,
                )
                logger.info("order_placed", order_id=order_id,
                           market=intent.market_name, side=intent.side,
                           direction=intent.direction, px=intent.price, sz=intent.size)
                return live
            else:
                logger.warning("order_rejected", resp=resp, market=intent.market_name)
                return None

        except Exception as e:
            logger.error("place_order_error", error=str(e), market=intent.market_name)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order. Non-blocking."""
        if self.cfg.dry_run:
            logger.info("dry_run_cancel", order_id=order_id)
            return True

        if self._client is None:
            return False

        try:
            resp = await asyncio.to_thread(self._client.cancel, order_id)
            # Polymarket retorna {'canceled': [order_id], 'not_canceled': {}}
            # (nao tem campo 'success')
            if isinstance(resp, dict):
                canceled_list = resp.get("canceled", [])
                success = (order_id in canceled_list) or bool(resp.get("success", False))
            else:
                success = bool(resp)
            if success:
                logger.info("order_cancelled", order_id=order_id)
            else:
                logger.warning("cancel_failed", order_id=order_id, resp=resp)
            return success
        except Exception as e:
            logger.error("cancel_error", order_id=order_id, error=str(e))
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders. Non-blocking."""
        if self.cfg.dry_run:
            logger.info("dry_run_cancel_all")
            return True

        if self._client is None:
            return False

        try:
            resp = await asyncio.to_thread(self._client.cancel_all)
            logger.info("cancel_all_done", resp=resp)
            return True
        except Exception as e:
            logger.error("cancel_all_error", error=str(e))
            return False

    async def get_order_book_async(self, token_id: str) -> Optional[dict]:
        """Get order book snapshot via REST. Non-blocking."""
        if self._client is None:
            return None
        try:
            return await asyncio.to_thread(self._client.get_order_book, token_id)
        except Exception as e:
            logger.error("get_book_error", token_id=token_id[:16], error=str(e))
            return None

    def get_order_book(self, token_id: str) -> Optional[dict]:
        """Get order book snapshot via REST. Blocking (for init only)."""
        if self._client is None:
            return None
        try:
            return self._client.get_order_book(token_id)
        except Exception as e:
            logger.error("get_book_error", token_id=token_id[:16], error=str(e))
            return None
