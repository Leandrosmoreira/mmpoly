"""Grid quote computation with inventory skew and time regime adjustment.

Grid dinamico 5x5:
- 5 niveis de BUY e 5 de SELL por token (UP e DOWN)
- Cada nivel = 5 shares (minimo Polymarket)
- Niveis ativos variam por regime de tempo e inventario
- Cancel seletivo: so cancela nivel se preco mudou >= 1 tick

Polymarket specifics:
- Minimum order size: 5 shares
- Tick size: 0.01
- Price range: 0.01 to 0.99
"""

from __future__ import annotations

from core.types import (
    BotConfig, Direction, GridConfig, Inventory, Quote, Side, TimeRegime, TopOfBook,
)

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


def compute_grid_quotes(
    book: TopOfBook,
    side: Side,
    inv: Inventory,
    regime: TimeRegime,
    cfg: BotConfig,
) -> list[Quote]:
    """Computa quotes do grid para um token (UP ou DOWN).

    Grid de compra: nivel 0 = bid+tick, nivel 1 = bid+tick-spacing, ...
    Grid de venda:  nivel 0 = ask-tick, nivel 1 = ask-tick+spacing, ...
    """
    if not book.is_valid:
        return []
    if regime == TimeRegime.EXIT:
        return []
    if book.spread < cfg.min_spread - 0.001:
        return []

    g = cfg.grid
    tick = cfg.tick
    spacing = g.level_spacing_ticks * tick  # ex: 2 * 0.01 = 0.02

    buy_levels, sell_levels = active_levels(cfg, regime, inv, side)

    current_pos = inv.shares_up if side == Side.UP else inv.shares_down
    quotes: list[Quote] = []

    # === Grid de COMPRA ===
    # Nivel 0: best_bid + tick (melhora 1 tick acima do melhor bid)
    # Nivel 1: nivel 0 - spacing
    # Nivel N: nivel 0 - N * spacing
    for lvl in range(buy_levels):
        px = round_price(book.best_bid + tick - lvl * spacing)

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
    for lvl in range(sell_levels):
        px = round_price(book.best_ask - tick + lvl * spacing)

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

    return quotes


def compute_all_quotes(
    book_up: TopOfBook,
    book_down: TopOfBook,
    inv: Inventory,
    regime: TimeRegime,
    cfg: BotConfig,
) -> list[Quote]:
    """Computa quotes do grid para UP e DOWN."""
    quotes: list[Quote] = []
    quotes.extend(compute_grid_quotes(book_up, Side.UP, inv, regime, cfg))
    quotes.extend(compute_grid_quotes(book_down, Side.DOWN, inv, regime, cfg))
    return quotes
