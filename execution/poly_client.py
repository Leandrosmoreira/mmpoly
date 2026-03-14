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
from py_clob_client.clob_types import (
    ApiCreds, AssetType, BalanceAllowanceParams, OpenOrderParams, OrderArgs, OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from core.types import BotConfig, Direction, Intent, LiveOrder, Side
from core.errors import ErrorCode

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
        self._last_place_error: str = ""  # "no_balance" when exchange rejects SELL
        self._approved_tokens: set[str] = set()  # BUG-020: tokens already approved

    def connect(self):
        """Initialize CLOB client with credentials from env.
        Accepts POLY_* or POLYMARKET_* (e.g. bookpoly .env)."""
        def _env(*keys: str) -> str:
            """Try multiple env var names, return first non-empty."""
            for k in keys:
                v = os.environ.get(k, "").strip()
                if v:
                    return v
            return ""
        private_key = _env("POLY_PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY")
        api_key = _env("POLY_API_KEY", "POLYMARKET_API_KEY")
        api_secret = _env("POLY_API_SECRET", "POLYMARKET_API_SECRET", "POLYMARKET_SECRET")
        api_passphrase = _env("POLY_API_PASSPHRASE", "POLYMARKET_PASSPHRASE", "POLYMARKET_API_PASSPHRASE")
        wallet_type = (os.environ.get("POLY_WALLET_TYPE") or os.environ.get("POLYMARKET_SIGNATURE_TYPE", "eoa")).lower().strip()

        logger.info("creds_check",
                     has_key=bool(private_key),
                     has_api_key=bool(api_key),
                     has_secret=bool(api_secret),
                     has_passphrase=bool(api_passphrase),
                     has_funder=bool(_env("POLY_FUNDER", "POLYMARKET_FUNDER")))
        if wallet_type == "1":
            wallet_type = "proxy"
        elif wallet_type == "2":
            wallet_type = "magic"
        elif wallet_type not in ("eoa", "proxy", "magic", "email", "magic2", "magiclink"):
            wallet_type = "eoa"
        self._funder = _env("POLY_FUNDER", "POLYMARKET_FUNDER")

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
                creds_raw = self._client.derive_api_key()
                # derive_api_key() pode retornar dict ou ApiCreds
                if isinstance(creds_raw, dict):
                    creds = ApiCreds(
                        api_key=creds_raw.get("apiKey", creds_raw.get("api_key", "")),
                        api_secret=creds_raw.get("secret", creds_raw.get("api_secret", "")),
                        api_passphrase=creds_raw.get("passphrase", creds_raw.get("api_passphrase", "")),
                    )
                else:
                    creds = creds_raw
                self._client.set_api_creds(creds)
                logger.info("api_creds_derived",
                           api_key=(creds.api_key[:8] + "...") if creds and creds.api_key else "")
            except Exception as e:
                logger.error("derive_api_key_failed", error=str(e),
                             error_code=ErrorCode.API_DERIVE_KEY_FAILED)
                raise

        try:
            self._client.get_ok()
            logger.info("poly_client_connected", wallet_type=wallet_type)
        except Exception as e:
            logger.error("connection_check_failed", error=str(e),
                         error_code=ErrorCode.API_CONNECTION_FAILED)

    async def approve_token(self, token_id: str) -> bool:
        """Approve a conditional token for trading (allowance).

        BUG-020: Polymarket requires token approval before SELL orders.
        Without approval, SELL orders fail with "not enough balance / allowance".
        This must be called once per token before any SELL order.

        Returns True if approval succeeded or was already done.
        """
        if token_id in self._approved_tokens:
            return True

        if self.cfg.dry_run:
            self._approved_tokens.add(token_id)
            logger.info("dry_run_token_approved", token_id=token_id[:16] + "...")
            return True

        if self._client is None:
            return False

        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            resp = await asyncio.to_thread(
                self._client.update_balance_allowance, params
            )
            self._approved_tokens.add(token_id)
            logger.info("token_approved", token_id=token_id[:16] + "...", resp=resp)
            return True
        except Exception as e:
            logger.error("token_approval_failed",
                        token_id=token_id[:16] + "...", error=str(e),
                        error_code=ErrorCode.TOKEN_APPROVAL_FAILED)
            return False

    async def place_order(self, intent: Intent, token_id: str) -> Optional[LiveOrder]:
        """Place a POST_ONLY limit order. Non-blocking.

        Sets self._last_place_error to "no_balance" if the exchange rejects
        with "not enough balance" — used by caller to detect phantom inventory.
        """
        self._last_place_error = ""
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
                logger.warning("order_rejected", resp=resp, market=intent.market_name,
                              side=intent.side, direction=intent.direction,
                              px=intent.price, sz=intent.size,
                              error_code=ErrorCode.ORDER_REJECTED)
                return None

        except Exception as e:
            err_str = str(e).lower()
            is_balance_error = ("not enough balance" in err_str
                                or "allowance" in err_str)

            if is_balance_error:
                if intent.direction == Direction.SELL:
                    # BUG-020: SELL failing = token approval needed.
                    # The Polymarket error "not enough balance / allowance"
                    # is ambiguous — for SELL it means token not approved.
                    self._last_place_error = "allowance"
                    if token_id not in self._approved_tokens:
                        logger.warning("auto_approve_retry",
                                      token_id=token_id[:16] + "...",
                                      market=intent.market_name,
                                      side=intent.side)
                        approved = await self.approve_token(token_id)
                        if approved:
                            return await self.place_order(intent, token_id)
                else:
                    # BUG-022: BUY failing = insufficient USDC collateral.
                    # Don't try token approval — the wallet just doesn't
                    # have enough USDC to place this order.
                    self._last_place_error = "no_balance"
                    logger.warning("insufficient_usdc",
                                  market=intent.market_name,
                                  side=intent.side.value if intent.side else "?",
                                  px=intent.price, sz=intent.size,
                                  cost=round(intent.price * intent.size, 2),
                                  error_code=ErrorCode.ORDER_PLACE_FAILED)
                    return None

            logger.error("place_order_error", error=str(e), market=intent.market_name,
                         side=intent.side, direction=intent.direction,
                         px=intent.price, sz=intent.size,
                         error_code=ErrorCode.ORDER_PLACE_FAILED)
            return None

    async def cancel_order(self, order_id: str) -> str:
        """Cancel a specific order. Non-blocking.

        Returns:
            "canceled" — successfully canceled
            "matched" — order was filled (infer fill)
            "gone"    — already canceled or not found
            "failed"  — actual failure
        """
        if self.cfg.dry_run:
            logger.info("dry_run_cancel", order_id=order_id)
            return "canceled"

        if self._client is None:
            return "failed"

        try:
            resp = await asyncio.to_thread(self._client.cancel, order_id)
            if isinstance(resp, dict):
                canceled_list = resp.get("canceled", [])
                not_canceled = resp.get("not_canceled", {})
                if order_id in canceled_list or resp.get("success"):
                    logger.info("order_cancelled", order_id=order_id)
                    return "canceled"
                if not_canceled:
                    reason = str(not_canceled).lower()
                    if "matched" in reason:
                        logger.info("order_matched", order_id=order_id)
                        return "matched"
                    if "already" in reason or "not found" in reason:
                        logger.info("order_already_gone", order_id=order_id)
                        return "gone"
            elif resp:
                logger.info("order_cancelled", order_id=order_id)
                return "canceled"
            logger.warning("cancel_failed", order_id=order_id, resp=resp,
                          error_code=ErrorCode.CANCEL_FAILED)
            return "failed"
        except Exception as e:
            err_msg = str(e).lower()
            if "matched" in err_msg:
                logger.info("order_matched", order_id=order_id, error=str(e))
                return "matched"
            if "not found" in err_msg or "already" in err_msg:
                logger.info("order_already_gone", order_id=order_id, error=str(e))
                return "gone"
            logger.error("cancel_error", order_id=order_id, error=str(e),
                         error_code=ErrorCode.CANCEL_ERROR)
            return "failed"

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
            logger.error("cancel_all_error", error=str(e),
                         error_code=ErrorCode.CANCEL_ALL_ERROR)
            return False

    async def get_order_book_async(self, token_id: str) -> Optional[dict]:
        """Get order book snapshot via REST. Non-blocking."""
        if self._client is None:
            return None
        try:
            return await asyncio.to_thread(self._client.get_order_book, token_id)
        except Exception as e:
            logger.error("get_book_error", token_id=token_id[:16], error=str(e),
                         error_code=ErrorCode.GET_BOOK_ERROR)
            return None

    def get_order_book(self, token_id: str) -> Optional[dict]:
        """Get order book snapshot via REST. Blocking (for init only)."""
        if self._client is None:
            return None
        try:
            return self._client.get_order_book(token_id)
        except Exception as e:
            logger.error("get_book_error", token_id=token_id[:16], error=str(e),
                         error_code=ErrorCode.GET_BOOK_ERROR)
            return None

    async def get_open_orders(self, market_id: str = "") -> list[str]:
        """Get open order IDs from the exchange. Non-blocking.

        BUG-014: Used for reconciliation — detect orphan orders that
        the bot lost track of but are still alive on the exchange.

        Args:
            market_id: optional condition_id to filter (empty = all)

        Returns:
            list of order_id strings currently open on the exchange.
        """
        if self.cfg.dry_run or self._client is None:
            return []

        try:
            params = OpenOrderParams(market=market_id) if market_id else None
            orders = await asyncio.to_thread(self._client.get_orders, params)
            return [
                o.get("id", o.get("order_id", ""))
                for o in (orders or [])
                if isinstance(o, dict) and o.get("status", "").lower() in ("live", "open", "active", "")
            ]
        except Exception as e:
            logger.error("get_open_orders_error", error=str(e),
                         error_code=ErrorCode.RECONCILE_ERROR)
            return []
