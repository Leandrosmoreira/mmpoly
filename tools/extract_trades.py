#!/usr/bin/env python3
"""
Extrai os trades executados (fills) dos logs do bot para mercados específicos.
Gera um JSON com todos os fills dos arquivos de log dos mercados indicados.

Uso:
  python tools/extract_trades.py [--markets ID1,ID2] [--logs DIR]
  Saída: logs/trades_<market_ids>.json (ex: logs/trades_1773513000_1773512100.json)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Mercados dos links: 1773513000, 1773512100
DEFAULT_MARKET_IDS = ("1773513000", "1773512100")

FILL_EVENTS = ("fill_detected", "fill_processed")


def load_events(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrai trades executados (fills) dos logs dos mercados."
    )
    parser.add_argument(
        "--markets",
        type=str,
        default=",".join(DEFAULT_MARKET_IDS),
        help="IDs de mercado separados por vírgula (default: 1773513000,1773512100)",
    )
    parser.add_argument("--logs", type=str, default=None, help="Diretório de logs")
    parser.add_argument("--from-trades-file", action="store_true", help="Ler também logs/trades.jsonl ou vps_trades.jsonl (fills globais)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    logs_dir = Path(args.logs) if args.logs else root / "logs"
    market_ids = [s.strip() for s in args.markets.split(",") if s.strip()] or list(DEFAULT_MARKET_IDS)

    # Coletar todos os eventos de fill (fill_detected tem mais campos; fill_processed tem order_id)
    raw: list[dict] = []
    for mid in market_ids:
        path = logs_dir / f"market_btc_15m_{mid}_events.jsonl"
        for ev in load_events(path):
            if ev.get("event") not in FILL_EVENTS:
                continue
            raw.append({**ev, "_market_id": mid})

    # Opcional: ler do arquivo global de trades (vps_trades ou trades.jsonl) e filtrar por mercado
    if args.from_trades_file:
        for name in ("vps_trades.jsonl", "trades.jsonl"):
            path = logs_dir / name
            for ev in load_events(path):
                if ev.get("event") not in FILL_EVENTS:
                    continue
                market = ev.get("market") or ev.get("name") or ""
                for mid in market_ids:
                    if mid in market or market.endswith(mid):
                        raw.append({**ev, "_market_id": mid})
                        break

    # Um trade por fill: preferir fill_detected, enriquecer com order_id do fill_processed
    by_key: dict[tuple, dict] = {}
    for ev in raw:
        ts = ev.get("timestamp") or ""
        market = ev.get("market") or ev.get("name") or ""
        side = ev.get("side") or ""
        px = ev.get("px")
        sz = ev.get("sz")
        key = (ts, market, side, px, sz)
        order_id = ev.get("order_id") or ""
        if ev.get("event") == "fill_detected":
            trade = {
                "timestamp": ts,
                "market": market,
                "market_id": ev.get("_market_id"),
                "order_id": order_id,
                "side": side,
                "direction": ev.get("direction"),
                "price": px,
                "size": sz,
                "is_maker": ev.get("is_maker"),
                "net": ev.get("net"),
                "realized_pnl": ev.get("realized_pnl"),
                "delta_pnl": ev.get("delta_pnl"),
            }
            by_key[key] = {k: v for k, v in trade.items() if v is not None}
        else:
            # fill_processed: mesclar order_id em registro existente ou criar novo
            if key in by_key:
                if order_id:
                    by_key[key]["order_id"] = order_id
            else:
                by_key[key] = {
                    "timestamp": ts,
                    "market": market,
                    "market_id": ev.get("_market_id"),
                    "order_id": order_id,
                    "side": side,
                    "direction": ev.get("direction"),
                    "price": px,
                    "size": sz,
                }
                by_key[key] = {k: v for k, v in by_key[key].items() if v is not None}

    all_trades = list(by_key.values())

    # Ordenar por timestamp
    all_trades.sort(key=lambda t: (t.get("timestamp") or ""))

    # Análise resumida
    by_market = {}
    total_size = 0.0
    total_pnl = 0.0
    for t in all_trades:
        m = t.get("market") or t.get("market_id") or "?"
        by_market[m] = by_market.get(m, 0) + 1
        total_size += float(t.get("size") or 0)
        delta = t.get("delta_pnl")
        if delta is not None:
            total_pnl += float(delta)

    analysis = {
        "by_market": by_market,
        "total_volume_shares": round(total_size, 2),
        "total_delta_pnl": round(total_pnl, 4) if all_trades else None,
    }

    out_name = f"trades_{'_'.join(market_ids)}.json"
    out_path = logs_dir / out_name
    logs_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "markets": market_ids,
        "total_trades": len(all_trades),
        "analysis": analysis,
        "trades": all_trades,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Escrito {len(all_trades)} trade(s) em {out_path}", file=sys.stderr)
    return result


if __name__ == "__main__":
    main()
