"""Pair / arbitrage detection for UP+DOWN books."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.types import BotConfig, TopOfBook


@dataclass
class PairSignal:
    """Signal to execute a pair trade."""
    edge: float        # expected profit per share
    size: float        # shares to trade
    ask_up: float      # price to buy UP
    ask_down: float    # price to buy DOWN
    bid_up: float      # price to sell UP
    bid_down: float    # price to sell DOWN
    direction: str     # "BUY_PAIR" or "SELL_PAIR"


def check_buy_pair(
    book_up: TopOfBook,
    book_down: TopOfBook,
    cfg: BotConfig,
) -> Optional[PairSignal]:
    """Check if buying both UP + DOWN is profitable.

    If ask_UP + ask_DOWN < 1.0 - fees, buying both guarantees profit
    since one will resolve to 1.0.
    """
    if not book_up.is_valid or not book_down.is_valid:
        return None

    cost = book_up.best_ask + book_down.best_ask
    edge = 1.0 - cost - cfg.fee_buffer

    if edge >= cfg.min_pair_edge:
        size = min(
            book_up.best_ask_sz,
            book_down.best_ask_sz,
            cfg.max_pair_size,
        )
        if size >= cfg.min_pair_size:
            return PairSignal(
                edge=edge,
                size=size,
                ask_up=book_up.best_ask,
                ask_down=book_down.best_ask,
                bid_up=book_up.best_bid,
                bid_down=book_down.best_bid,
                direction="BUY_PAIR",
            )
    return None


def check_sell_pair(
    book_up: TopOfBook,
    book_down: TopOfBook,
    cfg: BotConfig,
) -> Optional[PairSignal]:
    """Check if selling both UP + DOWN is profitable.

    If bid_UP + bid_DOWN > 1.0 + fees, selling both guarantees profit.
    Requires inventory on both sides.
    """
    if not book_up.is_valid or not book_down.is_valid:
        return None

    revenue = book_up.best_bid + book_down.best_bid
    edge = revenue - 1.0 - cfg.fee_buffer

    if edge >= cfg.min_pair_edge:
        size = min(
            book_up.best_bid_sz,
            book_down.best_bid_sz,
            cfg.max_pair_size,
        )
        if size >= cfg.min_pair_size:
            return PairSignal(
                edge=edge,
                size=size,
                ask_up=book_up.best_ask,
                ask_down=book_down.best_ask,
                bid_up=book_up.best_bid,
                bid_down=book_down.best_bid,
                direction="SELL_PAIR",
            )
    return None


def check_pair(
    book_up: TopOfBook,
    book_down: TopOfBook,
    cfg: BotConfig,
) -> Optional[PairSignal]:
    """Check for any pair opportunity."""
    signal = check_buy_pair(book_up, book_down, cfg)
    if signal:
        return signal
    return check_sell_pair(book_up, book_down, cfg)
