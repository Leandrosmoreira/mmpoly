# GabaBook MM Bot — Claude Code Instructions

## Project Overview

Polymarket market-making bot for BTC 15-minute UP/DOWN crypto markets.
- **Language:** Python 3.11+ (async, no Cython)
- **Strategy:** Spread capture + inventory control + time gating
- **Exchange:** Polymarket CLOB (POST_ONLY only, maker fee ~0%)
- **Deploy:** VPS via systemd (`botquant.service`)

## Project Structure

```
bot/          → Entry point, main loop, supervisor
core/         → Decision logic (engine, quoter, skew, types)
data/         → State (book cache, inventory, fills)
execution/    → Exchange I/O (API client, WS feeds, scanner, order manager)
risk/         → Kill switch, rate limits, PnL floor
tests/        → pytest unit tests (102 tests)
tools/        → CLI utilities (scan_markets, analyze_skew)
config/       → YAML config (bot.yaml, markets.yaml)
docs/         → Bug backlog (BUGS.md)
```

## Critical Rules

1. **Never break fill detection.** Fills are inferred from cancel "matched" responses. This is the ONLY fill path. No WS fill events exist.
2. **Always POST_ONLY.** Market orders only in kill switch scenarios.
3. **Intent pattern.** Engine returns Intent objects (pure data, no I/O). Executor handles API calls.
4. **Every bug fix needs a test.** No exceptions.
5. **Delta PnL.** Risk manager receives per-fill delta, never cumulative `realized_pnl`.
6. **Cancel-on-fill inline.** Process via deque in `_execute_intents()`, never `asyncio.create_task()`.
7. **Always commit + push** when making code changes (analyst reviews via GitHub).

## Running Tests

```bash
python -X utf8 -m pytest tests/ -v
```

All 102 tests must pass before any commit.

---

## Subagents

Four specialized agents are available in `.claude/agents/`:

| Agent | File | Specialty |
|-------|------|-----------|
| **strategy** | `strategy.md` | Quoting, pricing, skew, inventory logic, grid computation |
| **execution** | `execution.md` | Order lifecycle, fills, cancels, WS feeds, API, inventory state |
| **debug** | `debug.md` | Production logs, incident analysis, root cause identification |
| **testing** | `testing.md` | Unit tests, regression, edge cases, coverage |

---

## Routing Matrix

### By task type

| Task | Primary | Secondary | Dispatch |
|------|---------|-----------|----------|
| **Bug in production** (logs, kill switch, errors) | debug | execution, testing | Sequential: debug → fix → testing |
| **New indicator / skew change** | strategy | testing | Sequential: strategy → implement → testing |
| **Quoting logic change** (grid, soma, pricing) | strategy | testing | Sequential: strategy → implement → testing |
| **Order lifecycle change** (fills, cancels, intents) | execution | testing | Sequential: execution → implement → testing |
| **WebSocket / API change** | execution | debug | Sequential: execution → implement → testing |
| **Refactor** | strategy or execution | testing | Sequential: analyze → implement → testing |
| **Test gap / coverage improvement** | testing | — | Direct |

### By directory

| Directory | Primary Agent | Secondary |
|-----------|--------------|-----------|
| `core/` | strategy | testing |
| `execution/` | execution | debug, testing |
| `data/` | execution | testing |
| `risk/` | execution | testing |
| `bot/` | execution | debug |
| `tests/` | testing | — |
| `tools/` | — (direct) | — |
| `config/` | — (direct) | — |

### Dispatch rules

**Use parallel dispatch when:**
- Tasks are independent (e.g., strategy analysis + test design)
- Files don't overlap
- No shared state between analyses

**Use sequential dispatch when:**
- Debug must identify root cause before execution proposes fix
- Strategy must validate approach before testing designs test cases
- Result of one agent feeds the next

**Always end with testing** for any change to `core/`, `execution/`, `data/`, `risk/`, or `bot/`.
