"""Grid quote computation with inventory skew, time regime, and soma check.

Grid dinamico 5x5:
- 5 niveis de BUY e 5 de SELL por token (UP e DOWN)
- Cada nivel = 5 shares (minimo Polymarket)
- Niveis ativos variam por regime de tempo e inventario
- Cancel seletivo: so cancela nivel se preco mudou >= 1 tick
- Soma check: ajusta precos quando UP_mid + DOWN_mid diverge de 1.0

Polymarket specifics:
- Minimum order size: 5 shares
- Tick size: 0.01
- Price range: 0.01 to 0.99
"""

from __future__ import annotations

import structlog

from core.types import (
    BotConfig, Direction, GridConfig, Inventory, Quote, Side, SkewResult, TimeRegime, TopOfBook,
)

log = structlog.get_logger()

MIN_ORDER_SIZE = 5  # Polymarket minimum


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def round_price(px: float) -> float:
    """Round to 2 decimals and clamp to valid Poly range."""
    return clamp(round(px, 2), 0.01, 0.99)


def round_size(sz: float) -> float:
    """Round size to integer, enforce Poly minimum."""
    rounded = max(0, round(sz))
    return rounded if rounded >= MIN_ORDER_SIZE else 0


def active_levels(
    cfg: BotConfig,
    regime: TimeRegime,
    inv: Inventory,
    side: Side,
) -> tuple[int, int]:
    """Calcula quantos niveis de BUY e SELL ficam ativos.

    Retorna: (buy_levels, sell_levels)

    Logica de skew por inventario:
      - Se pesado UP (net > 0): reduz BUY UP, aumenta SELL UP
      - Se pesado DOWN (net < 0): reduz BUY DOWN, aumenta SELL DOWN
      Cada unidade de net_soft_limit/max_levels de net = remove 1 nivel de compra
    """
    g = cfg.grid
    net = inv.net  # positivo = pesado UP, negativo = pesado DOWN

    if regime == TimeRegime.EARLY:
        buy_l = g.early_buy_levels    # 1 — cauteloso
        sell_l = g.early_sell_levels  # 1

    elif regime == TimeRegime.MID:
        buy_l = g.mid_buy_levels      # 5 — grid completo
        sell_l = g.mid_sell_levels    # 5

    elif regime == TimeRegime.LATE:
        buy_l = 0                     # para de comprar
        sell_l = g.mid_sell_levels    # mantém grid de venda para desovar

    else:  # EXIT
        return 0, 0

    # Skew por inventario: remove niveis do lado pesado
    # unidade = net_soft_limit / max_levels (ex: 10 / 5 = 2 shares por nivel)
    unit = cfg.net_soft_limit / g.max_levels if g.max_levels > 0 else 1.0

    # Determina se este side/direction eh o lado "pesado"
    if side == Side.UP:
        heavy_buy = max(0.0, net) / unit    # pesado UP -> reduz BUY UP
        heavy_sell = max(0.0, -net) / unit  # pesado DOWN -> reduz SELL UP
    else:  # DOWN
        heavy_buy = max(0.0, -net) / unit   # pesado DOWN -> reduz BUY DOWN
        heavy_sell = max(0.0, net) / unit   # pesado UP -> reduz SELL DOWN

    buy_l = max(0, int(buy_l - heavy_buy))
    sell_l = max(0, int(sell_l - heavy_sell))

    return buy_l, sell_l


def compute_soma_adjustment(
    book_up: TopOfBook,
    book_down: TopOfBook,
    cfg: BotConfig,
) -> tuple[float, float]:
    """Calcula ajuste de preco por token baseado na divergencia da soma.

    UP_mid + DOWN_mid deveria ser ~1.0. Quando diverge:
    - soma > 1.0 (overpriced): adj positivo → BUYs mais baratos, SELLs mais caros
    - soma < 1.0 (underpriced): adj negativo → BUYs mais caros, SELLs mais baratos

    Returns: (up_adj, down_adj) — offset a aplicar nos precos de cada token.
    """
    sc = cfg.soma
    if not sc.enabled:
        return 0.0, 0.0

    if book_up.mid <= 0 or book_down.mid <= 0:
        return 0.0, 0.0

    soma = book_up.mid + book_down.mid
    divergence = soma - sc.fair_value

    if abs(divergence) < sc.threshold:
        return 0.0, 0.0

    # Distribui ajuste proporcionalmente ao mid de cada lado
    up_weight = book_up.mid / soma
    down_weight = book_down.mid / soma

    raw_adj = divergence * sc.aggression

    up_adj = clamp(raw_adj * up_weight, -sc.max_adjustment, sc.max_adjustment)
    down_adj = clamp(raw_adj * down_weight, -sc.max_adjustment, sc.max_adjustment)

    log.info("soma_check", soma=round(soma, 4), divergence=round(divergence, 4),
             up_adj=round(up_adj, 4), down_adj=round(down_adj, 4))

    return up_adj, down_adj


def compute_grid_quotes(
    book: TopOfBook,
    side: Side,
    inv: Inventory,
    regime: TimeRegime,
    cfg: BotConfig,
    price_adj: float = 0.0,
    skew_reservation: float = 0.0,
    skew_bid_adj: float = 0.0,
    skew_ask_adj: float = 0.0,
    suppress_buys: bool = False,
) -> list[Quote]:
    """Computa quotes do grid para um token (UP ou DOWN).

    Grid de compra: nivel 0 = bid+tick, nivel 1 = bid+tick-spacing, ...
    Grid de venda:  nivel 0 = ask-tick, nivel 1 = ask-tick+spacing, ...

    Price layers (applied in order):
    1. Grid base (bid/ask +/- tick +/- spacing)
    2. Soma check (price_adj): adj > 0 → BUY mais barato, SELL mais caro
    3. Skew reservation (skew_reservation): shifts center (both BUY and SELL)
    4. Skew side (skew_bid_adj / skew_ask_adj): asymmetric aggressiveness

    Sign convention for skew: adj > 0 = raise price, adj < 0 = lower price.
    """
    if regime == TimeRegime.EXIT:
        return []

    current_pos = inv.shares_up if side == Side.UP else inv.shares_down

    if not book.is_valid:
        # Can't compute full grid, but if we HAVE inventory and a bid price,
        # generate an emergency sell to avoid holding to expiry
        if book.best_bid > 0 and current_pos >= cfg.grid.level_size:
            log.info("emergency_sell", side=side.value,
                     best_bid=book.best_bid, pos=current_pos,
                     reason="book_invalid_but_has_inventory")
            return [Quote(
                side=side,
                direction=Direction.SELL,
                price=round_price(book.best_bid + cfg.tick),
                size=cfg.grid.level_size,
                level=0,
            )]
        return []

    if book.spread < cfg.min_spread - 0.001:
        return []

    g = cfg.grid
    tick = cfg.tick
    spacing = g.level_spacing_ticks * tick  # ex: 2 * 0.01 = 0.02

    buy_levels, sell_levels = active_levels(cfg, regime, inv, side)

    # Don't buy more when already holding a full level — sell first.
    # Prevents "not enough balance" spam when USDC is tied up in positions.
    if current_pos >= g.level_size:
        buy_levels = 0

    # Combined ask filter: no edge when UP_ask + DOWN_ask >= 1.0
    if suppress_buys:
        buy_levels = 0

    quotes: list[Quote] = []

    # === Grid de COMPRA ===
    # Nivel 0: best_bid + tick (melhora 1 tick acima do melhor bid)
    # Nivel 1: nivel 0 - spacing
    # Nivel N: nivel 0 - N * spacing
    # Soma check: price_adj > 0 → BUY mais barato (menos fills quando overpriced)
    # Skew: reservation shifts center, bid_adj adjusts buy aggressiveness
    for lvl in range(buy_levels):
        px = round_price(
            book.best_bid + tick - lvl * spacing
            - price_adj           # soma: adj > 0 → buy cheaper
            + skew_reservation    # skew: shifts center (positive = raise)
            + skew_bid_adj        # skew: buy-specific (positive = raise = more aggressive)
        )

        # POST_ONLY: nao pode cruzar o ask
        if px >= book.best_ask:
            px = round_price(book.best_bid - lvl * spacing)
        if px <= 0 or px >= book.best_ask:
            continue  # nivel invalido, pula

        # Limite de posicao: nao compra alem do max
        if current_pos + (lvl + 1) * g.level_size > cfg.max_position:
            break  # niveis mais distantes tambem estouram, pode parar

        quotes.append(Quote(
            side=side,
            direction=Direction.BUY,
            price=px,
            size=g.level_size,
            level=lvl,
        ))

    # === Grid de VENDA ===
    # Nivel 0: best_ask - tick (melhora 1 tick abaixo do melhor ask)
    # Nivel 1: nivel 0 + spacing
    # Nivel N: nivel 0 + N * spacing
    # Soma check: price_adj > 0 → SELL mais caro (mais fills quando overpriced)
    # Skew: reservation shifts center, ask_adj adjusts sell aggressiveness
    for lvl in range(sell_levels):
        px = round_price(
            book.best_ask - tick + lvl * spacing
            + price_adj           # soma: adj > 0 → sell more expensive
            + skew_reservation    # skew: shifts center (positive = raise)
            + skew_ask_adj        # skew: sell-specific (positive = raise = less aggressive)
        )

        # POST_ONLY: nao pode cruzar o bid
        if px <= book.best_bid:
            px = round_price(book.best_ask + lvl * spacing)
        if px <= 0 or px <= book.best_bid or px > 0.99:
            continue

        # So vende o que tem em inventario
        shares_needed = (lvl + 1) * g.level_size
        if current_pos < shares_needed:
            break  # sem shares para vender neste nivel

        quotes.append(Quote(
            side=side,
            direction=Direction.SELL,
            price=px,
            size=g.level_size,
            level=lvl,
        ))

    log.info("grid_computed",
             side=side.value, regime=regime.value,
             buy_levels=buy_levels, sell_levels=sell_levels,
             quotes=len(quotes), pos=current_pos,
             book_valid=book.is_valid, spread=round(book.spread, 4),
             bid=book.best_bid, ask=book.best_ask)

    return quotes


def compute_all_quotes(
    book_up: TopOfBook,
    book_down: TopOfBook,
    inv: Inventory,
    regime: TimeRegime,
    cfg: BotConfig,
    skew_up: SkewResult | None = None,
    skew_down: SkewResult | None = None,
) -> list[Quote]:
    """Computa quotes do grid para UP e DOWN com soma check + skew.

    Price layers applied in order:
    1. Grid base
    2. Soma check (price_adj)
    3. Skew reservation + bid/ask adjustments
    """
    up_adj, down_adj = compute_soma_adjustment(book_up, book_down, cfg)

    # Skew adjustments (default to zero if None or shadow mode)
    s_up = skew_up or SkewResult()
    s_dn = skew_down or SkewResult()

    # Combined ask filter: suppress buys when UP_ask + DOWN_ask is excessively above fair_value.
    # In a normal 1-tick spread market, combined asks ≈ 1.01-1.02 (fair_value + both spreads).
    # Only suppress when combined cost is significantly above fair_value (>= threshold),
    # indicating genuine mispricing rather than normal spread overhead.
    suppress_buys = False
    if (book_up.best_ask > 0 and book_down.best_ask > 0
            and cfg.soma.enabled):
        combined_ask = book_up.best_ask + book_down.best_ask
        # Threshold = fair_value + soma.threshold (default 0.03)
        # Allows normal 1-2 tick spreads, blocks when truly overpriced
        if combined_ask >= cfg.soma.fair_value + cfg.soma.threshold:
            suppress_buys = True
            log.info("buys_suppressed_no_edge",
                     up_ask=book_up.best_ask, down_ask=book_down.best_ask,
                     combined=round(combined_ask, 4),
                     threshold=round(cfg.soma.fair_value + cfg.soma.threshold, 4))

    quotes: list[Quote] = []
    quotes.extend(compute_grid_quotes(
        book_up, Side.UP, inv, regime, cfg,
        price_adj=up_adj,
        skew_reservation=s_up.reservation_adj,
        skew_bid_adj=s_up.bid_adj,
        skew_ask_adj=s_up.ask_adj,
        suppress_buys=suppress_buys,
    ))
    quotes.extend(compute_grid_quotes(
        book_down, Side.DOWN, inv, regime, cfg,
        price_adj=down_adj,
        skew_reservation=s_dn.reservation_adj,
        skew_bid_adj=s_dn.bid_adj,
        skew_ask_adj=s_dn.ask_adj,
        suppress_buys=suppress_buys,
    ))
    return quotes
