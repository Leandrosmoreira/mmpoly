# GabaBook MM Bot - Runbook Operacional

## 1. Kill Switch Triggered

**Sintomas:** Bot para de cotar, logs mostram `kill_switch` (E4001).

**Investigar:**
```bash
# Ver razao do kill
grep kill_switch logs/events.jsonl | tail -5

# Checar PnL diario
grep snapshot logs/pnl.jsonl | tail -1 | jq '.daily_pnl'

# Checar rejects
grep order_rejected logs/events.jsonl | wc -l
```

**Fix:**
- Se PnL: verificar se mercado teve movimento extremo. Considerar ajustar `max_daily_loss` no `config/bot.yaml`.
- Se rejects: verificar saldo USDC e credenciais API.
- Para resetar: reiniciar o bot (`sudo systemctl restart botquant`). O risk manager reseta no restart.

---

## 2. Bot Nao Coloca Ordens

**Sintomas:** Nenhum `order_placed` nos logs.

**Checklist:**
```bash
# Estado do bot
grep state_change logs/events.jsonl | tail -5

# Books validos?
grep grid_computed logs/events.jsonl | tail -5 | jq '{side, book_valid, spread}'

# Mercado ativo?
grep market_registered logs/events.jsonl | tail -3

# Regime de tempo?
grep tick_summary logs/events.jsonl | tail -5 | jq '{regime, state}'

# Risk bloqueando?
grep -E "kill_switch|hard_limit|cancel_rate" logs/events.jsonl | tail -5
```

**Causas comuns:**
- Book invalido (spread=0 ou bid_sz=0): WS desconectou → checar `ws_connected`
- Estado IDLE: books nao validaram ainda, esperar warmup
- Estado EXIT: mercado expirou, scanner deve encontrar proximo
- Hard limit: net > 25 shares, precisa desovar inventario manualmente

---

## 3. Phantom Fills

**Sintomas:** Inventario mostra shares que nao existem na exchange.

**Verificar nos logs:**
```bash
# Fills reais vs phantom
grep phantom_fill_blocked logs/events.jsonl | tail -10

# Comparar com fills aceitos
grep fill_detected logs/events.jsonl | tail -10 | jq '{side, direction, sz}'

# Inventario atual
grep snapshot logs/pnl.jsonl | tail -1 | jq '{pos_up, pos_down, net}'
```

**Fix:**
- Phantom fills ja sao bloqueados automaticamente (fix c82b77d).
- Se inventario estiver errado: parar bot, deletar `logs/inventory.json`, reiniciar.
- Verificar posicoes reais no site Polymarket.

---

## 4. WS Desconectou

**Sintomas:** `ws_error` (E1001) ou `ws_connection_error` (E1002) nos logs.

**Verificar:**
```bash
grep -E "ws_error|ws_connected|ws_reconnecting" logs/events.jsonl | tail -10
```

**Fix:**
- Auto-reconnect com backoff exponencial ja esta ativo.
- Se persistir: verificar conectividade do VPS (`ping clob.polymarket.com`).
- WS URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

---

## 5. Inventory Drift

**Sintomas:** Bot acha que tem shares mas ordens de venda falham com "not enough balance".

**Verificar:**
```bash
# Inventario local
cat logs/inventory.json | jq '.markets'

# Ordens rejeitadas
grep order_rejected logs/events.jsonl | tail -10 | jq '{side, direction, px, sz}'

# Fills recentes
grep fill_detected logs/events.jsonl | tail -20 | jq '{side, direction, sz}'
```

**Fix:**
1. Parar bot: `sudo systemctl stop botquant`
2. Verificar posicoes reais no Polymarket
3. Corrigir `logs/inventory.json` manualmente ou deletar para resetar
4. Reiniciar: `sudo systemctl start botquant`

---

## 6. Bot Crashou e Reiniciou

**O que checar:**
```bash
# Supervisor restart
grep -E "bot_starting|bot_shutdown|inventory_restored" logs/events.jsonl | tail -10

# Inventario foi restaurado?
grep inventory_restored logs/events.jsonl | tail -1

# Snapshot age (max 900s = 15min)
grep inventory_snapshot_too_old logs/events.jsonl | tail -1
```

**Notas:**
- Crash recovery restaura inventario do `logs/inventory.json` se < 15 min.
- Se snapshot velho demais: bot inicia com inventario zerado.
- Supervisor (`bot/supervisor.py`) reinicia com backoff exponencial.

---

## 7. Daily PnL Negativo

**Verificar:**
```bash
# PnL por mercado
grep snapshot logs/pnl.jsonl | jq '{market, realized_pnl, pos_up, pos_down}'

# Fills do dia
grep fill_detected logs/events.jsonl | jq '{side, direction, px, sz}' | head -50

# Custo medio vs preco de venda
grep fill_detected logs/events.jsonl | jq 'select(.direction=="SELL") | {side, px, sz}'
```

**Analise:**
- PnL = sum((sell_px - avg_cost) * sz) para cada sell
- Negativo geralmente indica: comprou caro e vendeu barato (spread colapsou)
- Checar se `min_spread` esta adequado no config

---

## Comandos Uteis

```bash
# Status do servico
sudo systemctl status botquant

# Logs em tempo real
journalctl -u botquant -f

# Ultimos erros
grep -E "error_code|level.*error" logs/events.jsonl | tail -20

# Resumo de um ciclo especifico
CYCLE="abc12345"
grep $CYCLE logs/events.jsonl | jq .

# Contar eventos por tipo
cat logs/events.jsonl | jq -r '.event' | sort | uniq -c | sort -rn | head -20
```
