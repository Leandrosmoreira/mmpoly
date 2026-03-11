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

### BUG-005: Excecoes silenciosas em logger e inventory
- **Severidade:** LOW
- **Arquivo:** `bot/logger.py:40`, `data/inventory.py:65`
- **Sintoma:** Erros de I/O em logging e snapshot persistence sao engolidos silenciosamente.
- **Causa raiz:** `except Exception: pass` original.
- **Fix:** Trocado por print para stderr. Implementado na Fase 1D.
- **Commit:** `793e3cc`
- **Status:** Resolved

### BUG-007: SELL spam por inventario fantasma
- **Severidade:** CRITICAL
- **Arquivo:** `bot/main.py`, `execution/poly_client.py`, `data/inventory.py`
- **Sintoma:** SELL DOWN a cada tick (~6s) com "not enough balance" (E2002) infinitamente. Bot nunca para de tentar.
- **Causa raiz:** Fill inferido de cancel "matched" cria `pos_down=5` no inventario local, mas exchange nao tem as shares. SELL falha, nunca eh registrado como live order, quoter gera sell_levels=1 novamente no proximo tick → loop infinito.
- **Trigger:** Bot reinicia com `inventory.json` corrompido (disco cheio) → fill inferred do primeiro cancel "matched" cria inventario fantasma.
- **Fix:** Quando SELL falha com "not enough balance", `poly_client._last_place_error` sinaliza "no_balance". `_execute_intents()` detecta e chama `inventory.zero_side()` para zerar o inventario fantasma daquele lado. Novo error code E4006 (PHANTOM_INVENTORY_ZEROED).
- **Status:** Resolved

### BUG-008: Stale book mata quoting silenciosamente
- **Severidade:** CRITICAL
- **Arquivo:** `core/engine.py`, `bot/main.py`
- **Sintoma:** Bot para de cotar apos ~60s. Nenhum erro nos logs. Apenas snapshots visiveis. Bot parece vivo mas nao coloca ordens.
- **Causa raiz:** WS do Polymarket so envia book updates quando orderbook muda. Em mercados quietos, `book.ts` fica congelado no valor do warmup REST. Apos 60s (`stale_book_ms`), `is_stale()=True`. Engine retorna `[]` sem nenhum log quando `has_inventory=False`.
- **Fix:** REST book refresh periodico (30s) no `_tick()` como fallback para WS silencioso. Log `stale_book_idle` no engine para visibilidade.
- **Status:** Resolved

### BUG-009: cancel-on-fill create_task bypasses fills_this_batch
- **Severidade:** CRITICAL
- **Arquivo:** `bot/main.py`
- **Sintoma:** Kill switch dispara por `consec_losses=6`. Phantom fills duplicados criam inventario fantasma → SELL falha → `phantom_inventory_zeroed` → contado como loss. `daily_pnl = -$2.15`.
- **Causa raiz:** `handle_fill()` usava `asyncio.create_task(self._execute_intents(cancel_intents))` para cancel-on-fill. O `create_task` cria uma chamada SEPARADA de `_execute_intents` com seu proprio `fills_this_batch` vazio. Quando a task roda (durante um `await` do batch pai), a mesma ordem pode retornar "matched" novamente — mas o `fills_this_batch` da task eh novo/vazio, entao o phantom fill nao eh bloqueado.
- **Sequencia:** BUY UP matched → fill → handle_fill → create_task(cancel BUY DOWN) → BUY DOWN matched no batch pai (phantom_fill_blocked ✓) → create_task roda → cancela BUY DOWN de novo → matched → fills_this_batch vazio → fill aceito ✗ → inventario fantasma.
- **Fix:** `handle_fill()` agora retorna cancel intents (em vez de create_task). `_execute_intents()` usa deque como queue e processa cancel-on-fill INLINE, compartilhando o mesmo `fills_this_batch`.
- **Status:** Resolved

### BUG-010: record_fill_pnl recebe PnL cumulativo em vez de delta
- **Severidade:** CRITICAL
- **Arquivo:** `bot/main.py`
- **Sintoma:** Kill switch dispara por `consec_losses=6` apos poucos fills. `daily_pnl` inflado (ex: -1.15 com perda real de -0.20). Bot para de operar desnecessariamente.
- **Causa raiz:** `handle_fill()` passava `inv.realized_pnl` (PnL cumulativo do mercado) para `risk_mgr.record_fill_pnl()`. Consequencias:
  1. `daily_pnl += cumulative` a cada fill → cresce geometricamente (ex: 5 fills × -0.20 = -1.00, mas perda real = -0.20)
  2. `if pnl < 0: consecutive_losses += 1` → BUYs (que nao geram realized PnL) contam como loss se o mercado tem PnL cumulativo negativo
  3. Kill switch dispara com `consec_losses > 5` muito rapido, mesmo sem perdas reais consecutivas
- **Fix:** Adicionado `self._last_pnl: dict[str, float]` para rastrear PnL anterior por mercado. `delta_pnl = inv.realized_pnl - prev_pnl` calculado e passado ao risk manager. Agora so conta loss quando o fill especifico gera PnL negativo. Log `fill_detected` inclui `delta_pnl`.
- **Status:** Resolved

## Abertos

### BUG-006: Sem reconciliacao com exchange
- **Severidade:** MEDIUM
- **Arquivo:** N/A (nao implementado)
- **Sintoma:** Inventario local pode divergir de posicoes reais sem deteccao.
- **Causa raiz:** Nao existe verificacao periodica contra REST API.
- **Fix planejado:** Fase 3 do plano de hardening — `_reconcile()` periodico.
- **Status:** Open (Fase 3 pendente)
