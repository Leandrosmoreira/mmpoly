"""Risk limit definitions and validation."""

from __future__ import annotations

from core.types import BotConfig, Inventory, MarketState


def check_position_limit(inv: Inventory, cfg: BotConfig) -> bool:
    """Check if any position exceeds max."""
    return inv.shares_up <= cfg.max_position and inv.shares_down <= cfg.max_position


def check_net_limit(inv: Inventory, cfg: BotConfig) -> str:
    """Check net exposure level.

    Returns: "ok", "soft", or "hard"
    """
    net = abs(inv.net)
    if net > cfg.net_hard_limit:
        return "hard"
    if net > cfg.net_soft_limit:
        return "soft"
    return "ok"


def should_quote_side(inv: Inventory, cfg: BotConfig, is_up: bool, is_buy: bool) -> bool:
    """Check if we should place a quote on this side/direction.

    Prevents adding to heavy side when at hard limit.
    """
    if abs(inv.net) <= cfg.net_hard_limit:
        return True

    # At hard limit: only allow reducing side
    if inv.net > 0:
        # Heavy UP: allow sell UP, buy DOWN
        if is_up and is_buy:
            return False
        if not is_up and not is_buy:
            return False
    else:
        # Heavy DOWN: allow sell DOWN, buy UP
        if not is_up and is_buy:
            return False
        if is_up and not is_buy:
            return False

    return True
