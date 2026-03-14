#!/usr/bin/env python3
"""
Analisa os logs do bot de market maker e gera um JSON apenas com erros + contexto,
nos 3 mercados dos links (1773487800, 1773489600, 1773488700). Sem filtro de tempo.

Uso:
  python tools/analyze_errors.py [--logs DIR] [--markets ID1,ID2,...]
  python tools/analyze_errors.py --window 60   # opcional: limitar a última N minutos
  Saída: logs/errors_last_hour.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# Mercados dos links: 1773487800, 1773489600, 1773488700
DEFAULT_MARKET_IDS = ("1773487800", "1773489600", "1773488700")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_traceback_for_file_line(exception_str: str | None) -> tuple[str | None, int | None]:
    """Extrai file e line do traceback em exception (último frame)."""
    if not exception_str or "File " not in exception_str:
        return None, None
    # Última linha "  File \"path\", line N, in ..."
    match = re.search(r'File "([^"]+)", line (\d+)', exception_str)
    if match:
        return match.group(1), int(match.group(2))
    return None, None


def _extract_error_type(msg: str | None, event: dict) -> str:
    if event.get("error_type"):
        return str(event["error_type"])
    if msg:
        # "PolyApiException[status_code=400, ...]" -> PolyApiException
        if "[" in msg:
            return msg.split("[")[0].strip()
        # "KeyError: 'best_bid'" -> KeyError
        if ":" in msg:
            return msg.split(":")[0].strip()
        if msg.startswith("'"):
            return "KeyError"
    return "Error"


def _event_summary(ev: dict) -> str:
    """Uma linha resumida do evento para recent_events."""
    e = ev.get("event", "")
    parts = [e]
    if ev.get("market"):
        parts.append(f"market={ev.get('market')}")
    if ev.get("state"):
        parts.append(f"state={ev.get('state')}")
    if ev.get("error"):
        parts.append(f"error={str(ev.get('error'))[:80]}")
    if ev.get("reason"):
        parts.append(f"reason={ev.get('reason')}")
    return " | ".join(parts)


def _build_bot_state(error_ev: dict, recent: list[dict]) -> dict:
    """Infer phase, market_id, inventory, open_orders dos eventos recentes ou do erro."""
    state = {}
    # Do próprio erro
    if error_ev.get("market"):
        state["market_id"] = error_ev.get("market")
    # Dos recentes (último tick_summary ou state)
    for ev in reversed(recent):
        if ev.get("event") == "tick_summary" and "state" in ev:
            state.setdefault("phase", ev.get("state", ""))
        if ev.get("event") == "tick_summary" and "net" in ev:
            state["inventory"] = ev.get("net")
        if ev.get("market") and "market_id" not in state:
            state["market_id"] = ev.get("market")
    if not state:
        state["phase"] = error_ev.get("event", "")
    return state


def load_events_from_files(
    log_paths: list[Path], since: datetime | None
) -> list[tuple[dict, int]]:
    """Carrega eventos de todos os arquivos, ordenados por tempo. Deduplica por (ts, event, market).
    Se since is None, não aplica filtro de tempo."""
    all_events: list[tuple[dict, int]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in log_paths:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(data.get("timestamp"))
                if since is not None and (not ts or ts < since):
                    continue
                key = (data.get("timestamp") or "", data.get("event") or "", str(data.get("market") or data.get("name") or ""))
                if key in seen:
                    continue
                seen.add(key)
                all_events.append((data, i))
    all_events.sort(key=lambda x: (x[0].get("timestamp") or ""))
    return all_events


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrai erros + contexto dos logs do bot nos 3 mercados dos links (sem filtro de hora)."
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Opcional: limitar a última N minutos. Se omitido, analisa todo o log dos mercados.",
    )
    parser.add_argument("--logs", type=str, default=None, help="Diretório de logs (default: raiz do repo/logs)")
    parser.add_argument(
        "--markets",
        type=str,
        default=",".join(DEFAULT_MARKET_IDS),
        help="IDs de mercado separados por vírgula (default: 1773487800,1773489600,1773488700)",
    )
    parser.add_argument("--all-markets", action="store_true", help="Incluir também vps_events.jsonl (todos os mercados)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    logs_dir = Path(args.logs) if args.logs else root / "logs"
    market_ids = tuple(s.strip() for s in args.markets.split(",") if s.strip()) or DEFAULT_MARKET_IDS

    # Por padrão: apenas os 3 arquivos de log por mercado (nada de vps_events, nada de janela de tempo)
    log_files = [logs_dir / f"market_btc_15m_{mid}_events.jsonl" for mid in market_ids]
    if args.all_markets:
        log_files.insert(0, logs_dir / "vps_events.jsonl")

    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=args.window)) if args.window is not None else None

    events_with_idx = load_events_from_files(log_files, since)
    if not events_with_idx and not args.all_markets:
        # Fallback: tentar vps_events se os arquivos por mercado não existirem
        alt = [logs_dir / "vps_events.jsonl"]
        events_with_idx = load_events_from_files(alt, since)
        if events_with_idx:
            market_ids = DEFAULT_MARKET_IDS  # filtrar por estes IDs ao buscar erros

    # Filtra apenas ERROR e CRITICAL (e, se veio do vps_events, só dos mercados indicados)
    error_levels = {"error", "critical"}
    error_indices: list[int] = []
    for idx, (ev, _) in enumerate(events_with_idx):
        if (ev.get("level") or "").lower() not in error_levels:
            continue
        if args.all_markets and market_ids:
            market = ev.get("market") or ev.get("name") or ""
            if not any(mid in market for mid in market_ids):
                continue
        error_indices.append(idx)

    errors_out: list[dict] = []
    for idx in error_indices:
        ev = events_with_idx[idx][0]
        recent = [events_with_idx[i][0] for i in range(max(0, idx - 10), idx)]
        recent_events = [_event_summary(r) for r in recent]

        exc_str = ev.get("exception") or ev.get("error") or ""
        err_msg = ev.get("error") or exc_str or ev.get("reason") or ev.get("event", "")
        if isinstance(err_msg, dict):
            err_msg = json.dumps(err_msg)[:500]
        else:
            err_msg = str(err_msg)[:500]

        file_path, line_num = _parse_traceback_for_file_line(ev.get("exception") or exc_str)
        if not file_path and exc_str:
            file_path, line_num = _parse_traceback_for_file_line(str(exc_str))

        code_context: dict = {
            "line_before": "",
            "error_line": err_msg[:300] if err_msg else "",
            "line_after": "",
        }
        if file_path and line_num is not None:
            code_context["error_line"] = f"{file_path}:{line_num}"

        entry = {
            "timestamp": ev.get("timestamp"),
            "market": ev.get("market") or ev.get("name") or "",
            "agent": ev.get("agent") or "",
            "error_type": _extract_error_type(err_msg, ev),
            "error_message": err_msg,
            "file": file_path,
            "line": line_num,
            "code_context": code_context,
            "bot_state": _build_bot_state(ev, recent),
            "recent_events": recent_events[-10:],
        }
        errors_out.append(entry)

    result = {
        "generated_at": now.isoformat(),
        "scope": "markets",
        "market_ids": list(market_ids),
        "window_minutes": args.window,
        "errors": errors_out,
    }
    out_path = logs_dir / "errors_last_hour.json"
    logs_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Escrito {len(errors_out)} erro(s) em {out_path} (mercados: {list(market_ids)})", file=sys.stderr)
    if not errors_out:
        print("Nenhum evento ERROR/CRITICAL nos logs dos 3 mercados.", file=sys.stderr)


if __name__ == "__main__":
    main()
