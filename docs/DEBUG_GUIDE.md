# GabaBook MM Bot - Debug Guide

## Estrutura dos Logs

O bot gera 3 arquivos de log em `logs/`:

| Arquivo | Conteudo | Uso |
|---|---|---|
| `events.jsonl` | Todos os eventos | Debug geral |
| `trades.jsonl` | Fills (fill_detected, fill_processed) | Analise de trades |
| `pnl.jsonl` | Snapshots periodicos | Monitoramento PnL |

Formato: JSON Lines (1 JSON por linha). Todos os eventos incluem:
- `timestamp` — ISO 8601 UTC
- `level` — info/warning/error/critical
- `event` — nome do evento (ex: `order_placed`, `fill_detected`)
- `cycle_id` — ID unico do tick (liga todos os eventos de um ciclo)

## Eventos Principais

### Ciclo de Vida
| Evento | Significado |
|---|---|
| `bot_starting` | Bot iniciou |
| `market_registered` | Mercado adicionado |
| `state_change` | Transicao de estado (IDLE/QUOTING/REBALANCING/EXITING) |
| `market_expired` | Mercado expirou |

### Decisoes
| Evento | Significado |
|---|---|
| `grid_computed` | Quoter calculou grid — mostra buy_levels, sell_levels, quotes, pos |
| `tick_summary` | Resumo do tick — place/cancel counts, state, regime |
| `soma_check` | Ajuste de preco por soma UP+DOWN |
| `emergency_sell` | Sell de emergencia com book invalido |

### Ordens
| Evento | Significado |
|---|---|
| `order_placed` | Ordem colocada com sucesso |
| `order_rejected` | Ordem rejeitada pela exchange (E2001) |
| `order_cancelled` | Ordem cancelada |
| `order_matched` | Cancel retornou "matched" = fill |
| `fill_detected` | Fill registrado no inventario |
| `fill_processed` | Cancel-on-fill executado |
| `phantom_fill_blocked` | Fill fantasma bloqueado (E4005) |

### Erros
| Evento | Codigo | Significado |
|---|---|---|
| `place_order_error` | E2002 | Falha ao colocar ordem |
| `cancel_error` | E2004 | Falha ao cancelar |
| `ws_error` | E1001 | WS desconectou |
| `hard_limit_breached` | E4004 | Net > hard_limit |
| `kill_switch` | E4001 | Kill switch ativado |

## Como Rastrear um Fill

```bash
# 1. Encontrar o fill
grep fill_detected logs/events.jsonl | tail -5

# 2. Pegar o cycle_id do fill
CYCLE=$(grep fill_detected logs/events.jsonl | tail -1 | jq -r '.cycle_id')

# 3. Ver todos os eventos daquele ciclo
grep "$CYCLE" logs/events.jsonl | jq .

# 4. Ver o cancel que gerou o fill (order_matched)
ORDER=$(grep fill_detected logs/events.jsonl | tail -1 | jq -r '.order_id')
grep "$ORDER" logs/events.jsonl | jq .

# 5. Ver cancel-on-fill resultante
grep fill_processed logs/events.jsonl | grep "$ORDER" | jq .
```

## Como Usar cycle_id

Cada `_tick()` gera um `cycle_id` unico (8 chars hex). Todos os logs downstream incluem esse ID automaticamente via `structlog.contextvars`.

```bash
# Reconstruir um tick completo
grep "a1b2c3d4" logs/events.jsonl | jq .

# Ver sequencia: grid_computed -> tick_summary -> order_placed/cancelled
grep "a1b2c3d4" logs/events.jsonl | jq '{event, side, direction, px}'
```

## Como Ler o Grid

O evento `grid_computed` mostra a decisao do quoter para cada side:

```json
{
  "event": "grid_computed",
  "side": "UP",
  "regime": "MID",
  "buy_levels": 0,
  "sell_levels": 1,
  "quotes": 1,
  "pos": 5,
  "book_valid": true,
  "spread": 0.05,
  "bid": 0.50,
  "ask": 0.55,
  "cycle_id": "a1b2c3d4"
}
```

Interpretacao:
- `buy_levels=0, pos=5`: nao compra porque ja tem >= level_size
- `sell_levels=1, quotes=1`: 1 sell quote gerado
- `spread=0.05`: spread atual (> min_spread=0.02, ok)

## Comandos jq Uteis

```bash
# Erros por codigo
cat logs/events.jsonl | jq -r 'select(.error_code) | .error_code' | sort | uniq -c | sort -rn

# PnL por mercado
grep snapshot logs/pnl.jsonl | jq -r '[.market, .realized_pnl] | @tsv' | sort -k2 -n

# Fills por lado
grep fill_detected logs/events.jsonl | jq -r '[.side, .direction, .px, .sz] | @tsv'

# Regime changes
grep tick_summary logs/events.jsonl | jq -r '[.timestamp, .market, .regime, .state] | @tsv'

# Ordens rejeitadas com detalhe
grep order_rejected logs/events.jsonl | jq '{market, side, direction, px, sz, resp}'
```
