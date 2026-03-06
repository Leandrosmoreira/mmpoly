"""Test de colocacao e cancelamento de ordem — mesmo codigo que o bot usa.

Fluxo identico ao bot:
  1. Descobre mercado BTC 15m via market_scanner
  2. Busca o order book via poly_client.get_order_book_async()
  3. Cria Intent com preco seguro (longe do mercado, nao vai bater)
  4. Chama poly_client.place_order(intent, token_id)  <- IGUAL ao bot
  5. Espera 3s
  6. Chama poly_client.cancel_order(order_id)          <- IGUAL ao bot

Uso:
    # Modo simulado (sem ordens reais) — padrao seguro
    python tools/test_order.py

    # Modo real (envia ordem de verdade para Polymarket)
    python tools/test_order.py --live
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import yaml
import argparse
from dotenv import load_dotenv

# Project root no path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)
load_dotenv(os.path.join(_root, ".env"))
# Permitir usar chaves do bookpoly (POLYMARKET_*)
_bookpoly_env = os.path.join(_root, "..", "bookpoly", ".env")
if os.path.isfile(_bookpoly_env):
    load_dotenv(_bookpoly_env)

from bot.logger import setup_logging
from core.types import BotConfig, Direction, GridConfig, Intent, IntentType, Side
from execution.poly_client import PolyClient
from execution.market_scanner import discover_all_active

import structlog
logger = structlog.get_logger()


def load_config(live: bool = False) -> BotConfig:
    """Carrega config/bot.yaml exatamente como o bot faz."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "bot.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}

    cfg = BotConfig()
    for k, v in data.items():
        if k == "grid" and isinstance(v, dict):
            grid_cfg = GridConfig()
            for gk, gv in v.items():
                if hasattr(grid_cfg, gk):
                    setattr(grid_cfg, gk, gv)
            cfg.grid = grid_cfg
        elif hasattr(cfg, k):
            setattr(cfg, k, v)

    if cfg.grid_levels > 0:
        n = cfg.grid_levels
        cfg.grid.max_levels = n
        cfg.grid.mid_buy_levels = n
        cfg.grid.mid_sell_levels = n
        cfg.max_orders_per_side = max(4, n * 2)
        cfg.max_position = n * cfg.grid.level_size * 2
        cfg.net_hard_limit = n * cfg.grid.level_size * 2.5
        cfg.net_soft_limit = n * cfg.grid.level_size

    # Flag --live desativa dry_run
    if live:
        cfg.dry_run = False

    return cfg


def _safe_buy_price(best_bid: float, tick: float = 0.01) -> float:
    """Preco de compra seguro: 10 ticks abaixo do melhor bid.

    Longe o suficiente para nunca bater acidentalmente.
    """
    price = round(best_bid - (tick * 10), 2)
    return max(0.02, min(price, 0.98))


def _print_section(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print('='*50)


async def run_test(live: bool):
    setup_logging("logs")

    mode = "REAL (--live)" if live else "SIMULADO (dry_run)"
    _print_section(f"TESTE DE ORDEM — {mode}")

    # 1. Carrega config
    cfg = load_config(live=live)
    print(f"  dry_run    : {cfg.dry_run}")
    print(f"  tick       : {cfg.tick}")
    print(f"  level_size : {cfg.grid.level_size} shares")

    # 2. Descobre mercado BTC 15m
    _print_section("1. Descobrindo mercado BTC 15m...")
    markets = await discover_all_active(coins=["btc"], intervals=["15m"])

    if not markets:
        print("  ERRO: Nenhum mercado BTC 15m ativo encontrado.")
        print("  Aguarde o proximo janela de 15 minutos e tente novamente.")
        return

    market = markets[0]
    print(f"  Mercado    : {market.name}")
    print(f"  Slug       : {market.slug}")
    print(f"  Token UP   : {market.token_up[:20]}...")
    print(f"  Token DOWN : {market.token_down[:20]}...")
    print(f"  Tempo rest.: {market.time_remaining:.0f}s")
    print(f"  Bid/Ask    : {market.best_bid} / {market.best_ask}")
    print(f"  Liquidez   : ${market.liquidity:,.0f}")

    if market.time_remaining < 60:
        print("\n  AVISO: Menos de 60s restantes — mercado prestes a fechar!")
        print("  Cancele o teste e aguarde o proximo janelo.")
        return

    if not market.accepting_orders:
        print("\n  ERRO: Mercado nao esta aceitando ordens agora.")
        return

    # 3. Conecta poly_client (IDENTICO ao bot)
    _print_section("2. Conectando ao Polymarket CLOB...")
    poly_client = PolyClient(cfg)
    poly_client.connect()

    if not cfg.dry_run and poly_client._client is None:
        print("  ERRO: Falha ao conectar. Verifique .env (POLY_PRIVATE_KEY, POLY_FUNDER).")
        return
    print(f"  Status: {'OK (cliente conectado)' if poly_client._client else 'OK (dry_run, sem cliente)'}")

    # 4. Busca order book para preco seguro (IDENTICO ao bot)
    _print_section("3. Buscando order book...")
    token_id = market.token_up
    book = await poly_client.get_order_book_async(token_id)

    if book:
        # Polymarket retorna bids em ordem crescente (pior→melhor)
        # O melhor bid e o ultimo elemento da lista
        bids = getattr(book, 'bids', []) or []
        best_bid = float(bids[-1].price) if bids else market.best_bid
        print(f"  Book OK. Melhor bid: {best_bid} ({len(bids)} niveis no book)")
    else:
        best_bid = market.best_bid or 0.50
        print(f"  Book nao disponivel. Usando bid da API: {best_bid}")

    buy_price = _safe_buy_price(best_bid, cfg.tick)
    print(f"  Preco de teste (10 ticks abaixo): {buy_price}")

    # 5. Cria Intent — IDENTICO ao que o engine.py retorna
    _print_section("4. Criando Intent (igual ao bot)...")
    intent = Intent(
        type=IntentType.PLACE_ORDER,
        market_name=market.name,
        side=Side.UP,
        direction=Direction.BUY,
        price=buy_price,
        size=cfg.grid.level_size,  # 5 shares (minimo Polymarket)
        reason="test_order",
        level=0,
    )
    print(f"  Intent: {intent.direction} {intent.size} shares @ {intent.price}")
    print(f"  Side  : {intent.side}  (token UP = Yes)")
    print(f"  Reason: {intent.reason}")

    # 6. COLOCA ORDEM — IDENTICO ao bot/main.py
    _print_section("5. Colocando ordem...")
    if cfg.dry_run:
        print("  [DRY_RUN] Simulando place_order...")

    live_order = await poly_client.place_order(intent, token_id)

    if live_order is None:
        print("  ERRO: place_order retornou None. Verifique credenciais.")
        return

    print(f"  OK! order_id : {live_order.order_id}")
    print(f"  price        : {live_order.price}")
    print(f"  size         : {live_order.size}")
    print(f"  direction    : {live_order.direction}")
    print(f"  side         : {live_order.side}")
    print(f"  placed_at    : {time.strftime('%H:%M:%S', time.localtime(live_order.placed_at))}")

    # 7. Espera 3 segundos
    _print_section("6. Aguardando 3s antes de cancelar...")
    for i in range(3, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        await asyncio.sleep(1)
    print("  Cancelando agora!      ")

    # 8. CANCELA ORDEM — IDENTICO ao bot/main.py
    _print_section("7. Cancelando ordem...")
    if cfg.dry_run:
        print("  [DRY_RUN] Simulando cancel_order...")

    success = await poly_client.cancel_order(live_order.order_id)

    if success:
        print(f"  OK! Ordem {live_order.order_id} cancelada com sucesso.")
    else:
        print(f"  FALHA ao cancelar ordem {live_order.order_id}.")

    # Resultado final
    _print_section("RESULTADO")
    print(f"  Modo          : {mode}")
    print(f"  Mercado       : {market.name}")
    print(f"  Token testado : UP ({token_id[:20]}...)")
    print(f"  Preco         : {buy_price}")
    print(f"  order_id      : {live_order.order_id}")
    print(f"  Colocada      : OK")
    print(f"  Cancelada     : {'OK' if success else 'FALHOU'}")
    print()

    if cfg.dry_run:
        print("  Para testar com ordens REAIS: python tools/test_order.py --live")
    else:
        print("  Teste completo com ordens reais!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test place + cancel order via PolyClient")
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Envia ordens reais para Polymarket (padrao: dry_run simulado)",
    )
    args = parser.parse_args()

    asyncio.run(run_test(live=args.live))
