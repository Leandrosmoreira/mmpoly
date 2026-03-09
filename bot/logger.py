"""Structured logging setup with file output."""

from __future__ import annotations

import json
import sys
import structlog
from pathlib import Path


_log_files = {}


def setup_logging(log_dir: str = "logs"):
    """Configure structlog for JSON output to files and console."""
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # Open file handles (kept globally so they don't get GC'd)
    _log_files["events"] = open(log_path / "events.jsonl", "a", buffering=1)
    _log_files["trades"] = open(log_path / "trades.jsonl", "a", buffering=1)
    _log_files["pnl"] = open(log_path / "pnl.jsonl", "a", buffering=1)

    def file_writer(_, __, event_dict):
        """Write events to appropriate log files."""
        try:
            line = json.dumps(event_dict, default=str) + "\n"

            # Everything goes to events
            _log_files["events"].write(line)

            # Fills go to trades
            event = event_dict.get("event", "")
            if event in ("fill", "fill_processed"):
                _log_files["trades"].write(line)

            # Snapshots go to pnl
            if event == "snapshot":
                _log_files["pnl"].write(line)
        except Exception as e:
            import sys
            print(f"LOGGING_ERROR: {e}", file=sys.stderr)

        return event_dict

    # Build processor chain
    processors = [
        structlog.contextvars.merge_contextvars,  # auto-inject cycle_id etc.
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        file_writer,
    ]

    # Console output: pretty if terminal, JSON if piped/journald
    if sys.stdout.isatty():
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def log_snapshot(market_name: str, inv, risk_status: dict):
    """Log a PnL snapshot."""
    logger = structlog.get_logger()
    logger.info("snapshot",
                market=market_name,
                net=inv.net,
                pos_up=inv.shares_up,
                pos_down=inv.shares_down,
                realized_pnl=inv.realized_pnl,
                daily_pnl=risk_status.get("daily_pnl", 0),
                is_killed=risk_status.get("is_killed", False))
