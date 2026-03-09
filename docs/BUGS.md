# GabaBook MM Bot - Bug Backlog

## Resolvidos

### BUG-001: Sell paths bloqueados por book.is_valid
- **Severidade:** CRITICAL
- **Arquivo:** `core/engine.py`, `core/quoter.py`, `core/types.py`
- **Sintoma:** Bot nunca vendia apesar de ter inventario (pos_up=5, pos_down=5)
- **Causa raiz:** `is_valid` exigia `best_bid_sz > 0` E `best_ask_sz > 0`. Para vender so precisa de bid.
- **Fix:** Adicionado `has_bid` property em TopOfBook. Exit/stale paths usam `has_bid`. Emergency sell no quoter.
- **Commit:** `7b754ca`
- **Status:** Resolved

### BUG-002: Phantom fills por TTL batch cancel
- **Severidade:** HIGH
- **Arquivo:** `bot/main.py`
- **Sintoma:** Inventario mostra shares em ambos os lados, mas so um lado eh real. Venda de um lado falha.
- **Causa raiz:** TTL expira BUY UP e BUY DOWN juntos. Ambos retornam "matched" no cancel. Bot registra fill duplo.
- **Fix:** `fills_this_batch` set limita 1 fill por mercado por batch de cancel.
- **Commit:** `c82b77d`
- **Status:** Resolved

### BUG-003: "not enough balance" spam
- **Severidade:** MEDIUM
- **Arquivo:** `core/quoter.py`
- **Sintoma:** ~2 erros/segundo de "not enough balance" nos logs de producao.
- **Causa raiz:** Com pos=5 e net=0, skew nao suprime buys. Bot tenta comprar mas USDC ta preso em posicoes.
- **Fix:** `if current_pos >= g.level_size: buy_levels = 0` — vende primeiro antes de comprar mais.
- **Commit:** `c82b77d`
- **Status:** Resolved

### BUG-004: Logs de erro sem contexto
- **Severidade:** LOW
- **Arquivo:** `execution/poly_client.py`
- **Sintoma:** `place_order_error` e `order_rejected` so logavam market name, sem side/direction/price.
- **Causa raiz:** Campos faltando no structlog.
- **Fix:** Adicionado side, direction, px, sz em todos os logs de erro.
- **Commit:** `c82b77d`
- **Status:** Resolved

## Abertos

### BUG-005: Excecoes silenciosas em logger e inventory
- **Severidade:** LOW
- **Arquivo:** `bot/logger.py:40`, `data/inventory.py:65`
- **Sintoma:** Erros de I/O em logging e snapshot persistence sao engolidos silenciosamente.
- **Causa raiz:** `except Exception: pass` original.
- **Fix:** Trocado por print para stderr. Implementado na Fase 1D.
- **Status:** Resolved

### BUG-006: Sem reconciliacao com exchange
- **Severidade:** MEDIUM
- **Arquivo:** N/A (nao implementado)
- **Sintoma:** Inventario local pode divergir de posicoes reais sem deteccao.
- **Causa raiz:** Nao existe verificacao periodica contra REST API.
- **Fix planejado:** Fase 3 do plano de hardening — `_reconcile()` periodico.
- **Status:** Open (Fase 3 pendente)
