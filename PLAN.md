# PLAN.md — GabaBook MM Bot (Polymarket)

## 0) Objetivo

Bot market-maker estilo Gabagool para Polymarket CLOB.
Lucra com **spread capture** + **inventory control** + **time gating**.
Não prevê direção. Python puro async (sem Cython — latência da Poly não justifica).

### Edge Sources (ordem de importância)
1. **Spread capture** — maker ganha rebate, taker paga fee
2. **Inventory skew** — ajusta quotes para reduzir exposição
3. **Time regime** — opera agressivo no mid, conservador no early/late
4. **Pair edge** — raro mas captura quando ask_UP + ask_DOWN < 1 - fees

### Por que NÃO Cython
- WS da Poly atualiza a cada ~100-500ms
- Rate limit da API é 100 req/s (cancel/replace limitado)
- Gargalo é I/O, não compute
- Python asyncio com numpy para math é suficiente
- Elimina complexidade de build em produção

---

## 1) Princípios

### 1.1 Maker-first
- **Tudo POST_ONLY**. Market order APENAS em kill switch.
- Poly cobra ~2% taker fee, ~0% maker fee. Maker é o edge.

### 1.2 Cancel-on-fill (CRÍTICO)
- Quando lado UP filla, cancela quotes correspondentes no DOWN imediatamente.
- Evita ficar com posição dupla (long UP + long DOWN = locked capital sem edge).

### 1.3 Neutralidade por inventário
- Target: `net_exposure ≈ 0` (shares UP - shares DOWN)
- Skew quotes para empurrar inventário de volta ao neutro
- Hard limit: para de quotar se `|net| > hard_limit`

### 1.4 Rate limit aware
- Poly rate limit: ~100 req/s global
- Não cancelar/replacer a cada tick. TTL mínimo de 5s por quote.
- Batch cancels quando possível.

### 1.5 Time regimes
| Regime | time_remaining | Comportamento |
|--------|---------------|---------------|
| EARLY  | > 5 min       | Spread largo, size pequeno, paciente |
| MID    | 1-5 min       | Spread apertado, size normal, agressivo |
| LATE   | 30s-1 min     | Só reduz inventário, não abre novo |
| EXIT   | < 30s         | Cancel all, market exit se necessário |

---

## 2) Stack

```
Python 3.11+
asyncio (event loop)
aiohttp (REST + WS)
py-clob-client (Polymarket SDK)
py-order-utils (signing)
pydantic (config validation)
structlog (JSON logging)
```

---

## 3) Estrutura do Projeto

```
gababot/
  PLAN.md
  requirements.txt
  setup.py

  config/
    bot.yaml              # parâmetros do bot
    markets.yaml          # mercados para operar

  bot/
    __init__.py
    main.py               # entry point, asyncio.run
    supervisor.py          # watchdog, restart logic

  core/
    __init__.py
    engine.py              # state machine + decision loop
    quoter.py              # quote computation + skew
    pair.py                # pair/arb detection
    types.py               # dataclasses

  execution/
    __init__.py
    poly_client.py         # Polymarket CLOB wrapper
    order_manager.py       # order lifecycle, cancel-on-fill
    ws_feed.py             # WebSocket book feed

  data/
    __init__.py
    book.py                # order book cache (top-of-book)
    inventory.py           # position tracking
    fills.py               # fill history

  risk/
    __init__.py
    manager.py             # kill switch, limits, sanity
    limits.py              # parameter definitions

  logs/
    events.jsonl
    trades.jsonl
    pnl.jsonl

  services/
    botquant.service       # systemd unit
    .env.example
```

---

## 4) Modelo de Dados

### 4.1 TopOfBook (por token_id — UP ou DOWN)
```python
@dataclass
class TopOfBook:
    token_id: str
    best_bid: float      # melhor bid price
    best_bid_sz: float   # size no best bid
    best_ask: float      # melhor ask price
    best_ask_sz: float   # size no best ask
    mid: float           # (bid + ask) / 2
    spread: float        # ask - bid
    ts: float            # timestamp do último update
```

### 4.2 MarketState
```python
@dataclass
class MarketState:
    condition_id: str
    token_up: str         # token_id do YES
    token_down: str       # token_id do NO
    book_up: TopOfBook
    book_down: TopOfBook
    time_remaining_s: float
    end_ts: float
    regime: TimeRegime    # EARLY/MID/LATE/EXIT
    is_active: bool
    cooldown_until: float
```

### 4.3 Inventory
```python
@dataclass
class Inventory:
    shares_up: float = 0
    shares_down: float = 0
    avg_cost_up: float = 0
    avg_cost_down: float = 0
    realized_pnl: float = 0

    @property
    def net(self) -> float:
        return self.shares_up - self.shares_down
```

---

## 5) State Machine (por mercado)

```
IDLE → QUOTING → [REBALANCING] → QUOTING → ... → EXITING → IDLE
         ↓
    PAIR_OPPORTUNITY (raro)
```

### Estados:

**IDLE**
- Mercado não atende condições mínimas
- Condições para sair: mercado ativo, book saudável, time in range

**QUOTING**
- Posta quotes nos dois lados (UP bid/ask + DOWN bid/ask)
- Monitora fills e ajusta
- Transição para REBALANCING se |net| > soft_limit

**REBALANCING**
- Skew agressivo nos quotes
- Se |net| > hard_limit: para de quotar lado que aumenta exposição
- Volta para QUOTING quando |net| < soft_limit * 0.5

**PAIR_OPPORTUNITY**
- Detectou ask_up + ask_down < 1 - min_edge
- Tenta comprar ambos (raro, execução difícil)
- Volta para QUOTING

**EXITING**
- time_remaining < T_LATE ou kill switch
- Cancel all open orders
- Tenta reduzir inventário com limit agressivo
- Se time_remaining < T_EXIT: market exit se necessário

---

## 6) Engine de Quoting

### 6.1 Quote base
```python
def compute_quotes(book: TopOfBook, inv: Inventory, cfg: Config) -> Quotes:
    tick = cfg.tick  # 0.01

    # Base quotes: inside the spread
    buy_px = book.best_bid + tick  # improve best bid
    sell_px = book.best_ask - tick  # improve best ask

    # Don't cross book (POST_ONLY will reject anyway)
    if buy_px >= book.best_ask:
        buy_px = book.best_bid  # join bid instead
    if sell_px <= book.best_bid:
        sell_px = book.best_ask  # join ask instead

    # Minimum spread check
    if sell_px - buy_px < cfg.min_spread:
        return None  # skip, spread too tight
```

### 6.2 Inventory skew
```python
    # Skew: adjust quotes based on inventory
    skew = clamp(inv.net / cfg.net_soft_limit, -1.0, 1.0)

    # Positive net (heavy UP) → want to sell UP / buy DOWN
    buy_px  -= skew * tick * cfg.skew_factor  # less aggressive buying
    sell_px -= skew * tick * cfg.skew_factor  # more aggressive selling

    # Size skew: reduce size on heavy side
    buy_sz  = cfg.order_size * (1.0 - max(0, skew) * 0.5)
    sell_sz = cfg.order_size * (1.0 + max(0, skew) * 0.5)
```

### 6.3 Time regime adjustment
```python
    # Regime modifiers
    if regime == EARLY:
        buy_px  -= tick  # wider spread
        sell_px += tick
        buy_sz  *= 0.5   # smaller size
        sell_sz *= 0.5
    elif regime == LATE:
        # Only quotes that reduce inventory
        if inv.net > 0:
            buy_sz = 0   # don't buy more UP
        elif inv.net < 0:
            sell_sz = 0  # don't sell more UP
```

---

## 7) Pair / Arb Detection

### Realidade sobre pair edge na Poly
- Taker fee ~2% torna arb raro
- ask_up + ask_down precisa ser < 0.96 para ter edge (com fees)
- Quando existe, dura milliseconds
- Execução parcial é o caso comum (fill um lado, miss o outro)

### Implementação conservadora
```python
def check_pair(book_up: TopOfBook, book_down: TopOfBook, cfg: Config) -> PairSignal | None:
    cost = book_up.best_ask + book_down.best_ask
    edge = 1.0 - cost - cfg.fee_buffer

    if edge >= cfg.min_pair_edge:
        # Existe edge teórico
        size = min(book_up.best_ask_sz, book_down.best_ask_sz, cfg.max_pair_size)
        if size >= cfg.min_pair_size:
            return PairSignal(edge=edge, size=size)
    return None
```

**Regra: pair trade só com POST_ONLY.** Se não conseguir maker nos dois lados, não faz.

---

## 8) Order Manager (cancel-on-fill)

### Lifecycle de uma quote
```
PLACE (POST_ONLY) → LIVE → FILLED/PARTIAL/CANCELLED
                      ↓
                   TTL expired → CANCEL → RE-QUOTE
```

### Cancel-on-fill logic
```python
async def on_fill(self, fill: Fill):
    # Update inventory
    self.inventory.apply_fill(fill)

    # If UP filled, cancel corresponding DOWN orders
    if fill.token_id == self.market.token_up:
        await self.cancel_side_orders(self.market.token_down, fill.side)

    # If net exceeded, trigger rebalance
    if abs(self.inventory.net) > self.cfg.net_soft_limit:
        self.engine.transition(State.REBALANCING)

    # Re-quote with updated inventory
    self.engine.request_requote()
```

### TTL management
- Cada ordem tem TTL de `cfg.quote_ttl_ms` (default: 5000ms)
- Após TTL: cancel + re-quote com preço atualizado
- Se book não mudou: extend TTL (não cancel desnecessário)
- Rate limit budget: max `cfg.max_cancel_per_min` cancels/min

---

## 9) Risk Manager

### 9.1 Limites
| Param | Default | Descrição |
|-------|---------|-----------|
| max_position | 50 | shares max por token |
| net_soft_limit | 15 | trigger rebalance |
| net_hard_limit | 30 | stop quoting heavy side |
| max_orders_per_side | 2 | ordens ativas por lado |
| max_cancel_per_min | 60 | rate limit safety |
| max_daily_loss | -5.0 | USDC, kill switch |
| stale_book_ms | 5000 | book sem update = stale |

### 9.2 Kill switch triggers
```python
def check_kill(self) -> bool:
    return any([
        self.daily_pnl < self.cfg.max_daily_loss,        # PnL floor
        self.stale_count > self.cfg.max_stale,            # data quality
        self.reject_count > self.cfg.max_rejects,         # API issues
        self.consecutive_losses > self.cfg.max_consec,    # regime change
    ])
```

Kill switch action:
1. Cancel ALL open orders (all markets)
2. Set cooldown (30 min default)
3. Log reason
4. Alert (optional: telegram/discord webhook)

---

## 10) Logging

### Structured JSON logging (structlog)
Cada evento é uma linha JSONL:

```json
{"ts": 1709567890.123, "event": "quote_placed", "market": "btc-5min",
 "side": "UP", "direction": "BUY", "px": 0.52, "sz": 5,
 "net": 3, "regime": "MID", "spread": 0.02}

{"ts": 1709567891.456, "event": "fill", "market": "btc-5min",
 "side": "UP", "direction": "BUY", "px": 0.52, "sz": 5,
 "net": 8, "realized_pnl": 0.0, "unrealized_pnl": -0.10}

{"ts": 1709567900.000, "event": "snapshot", "market": "btc-5min",
 "net": 8, "pos_up": 15, "pos_down": 7,
 "realized_pnl": 1.23, "unrealized_pnl": -0.10}
```

### Log files
- `logs/events.jsonl` — tudo
- `logs/trades.jsonl` — fills only
- `logs/pnl.jsonl` — snapshots periódicos (cada 30s)

---

## 11) Config Defaults

```yaml
# bot.yaml
tick: 0.01
order_size: 5
min_spread: 0.02        # não opera se spread < 2 ticks
quote_ttl_ms: 5000      # 5s per quote
skew_factor: 2.0        # multiplier do inventory skew

net_soft_limit: 15
net_hard_limit: 30
max_position: 50
max_orders_per_side: 2
max_cancel_per_min: 60
max_daily_loss: -5.0

# Time regimes (seconds remaining)
t_early: 300             # > 5min
t_mid: 60                # 1-5min
t_late: 30               # 30s-1min
t_exit: 15               # < 15s, emergency exit

# Pair
min_pair_edge: 0.02      # 2% mínimo (conservador)
fee_buffer: 0.02         # 2% taker fee
max_pair_size: 10
min_pair_size: 2

# Risk
stale_book_ms: 5000
max_rejects: 10
max_consecutive_losses: 5
cooldown_s: 1800         # 30 min
```

---

## 12) Plano de Implementação

### Fase 1 — Skeleton + dry-run (ESTE ENTREGÁVEL)
1. Estrutura de projeto completa
2. Conexão WS + book cache
3. Engine retorna intents (não envia ordens)
4. Loga intents + book state
5. **Objetivo: ver o bot "pensando" ao vivo**

### Fase 2 — Ordens reais, risco baixo
1. Habilitar 1 mercado (BTC short-term)
2. `order_size=1`, `max_position=5`
3. Apenas QUOTING + REBALANCING (sem pair)
4. **Objetivo: validar fills + cancel/replace + inventory tracking**

### Fase 3 — Multi-mercado + pair
1. 4-8 mercados simultâneos
2. Pair detection ativo
3. Kill switch completo
4. **Objetivo: escala e robustez**

### Fase 4 — Otimização
1. Tuning de parâmetros via logs
2. Análise de fill_rate vs spread
3. Regime detection refinado
4. **Objetivo: maximizar PnL/risco**

---

## 13) Métricas de Sucesso

- `fill_rate` — fills/min por mercado (target: >2)
- `spread_capture_pnl` — PnL do maker (deve ser >70% do total)
- `inventory_cost` — PnL perdido por exposição
- `time_over_limit` — % tempo com |net| > soft_limit (target: <20%)
- `cancel_rate` — cancels/min (target: <max_cancel_per_min)
- `uptime` — % tempo em QUOTING state

---

## 14) O que NÃO fazer

- ❌ Cython/C para algo com 100ms+ de latência WS
- ❌ Market orders como rotina (2% fee destrói edge)
- ❌ Operar sem time gating (perto da resolução = binário)
- ❌ Confiar em pair edge como source primária de PnL
- ❌ Cancel/replace a cada tick (rate limit ban)
- ❌ Operar mercados com spread = 1 tick (sem edge)
- ❌ Deixar net sem hard limit (ruína garantida)
- ❌ Ignorar partial fills (inventário diverge)
