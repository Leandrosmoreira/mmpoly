# GabaBook MM Bot — Polymarket Market Maker

Grid dinâmico 5×5 para mercados BTC Up/Down 15min no Polymarket CLOB.

---

## Pré-requisitos

- VPS Ubuntu 22.04 / Debian 12 (mínimo 1 vCPU, 512MB RAM)
- Python 3.11+
- Conta Polymarket com Magic Link (email wallet)
- USDC na carteira Polymarket (mínimo recomendado: $50)

---

## Deploy via GitHub (recomendado)

### Passo A — Subir o código para o GitHub (no seu PC)

Abra o **Git Bash** ou **PowerShell** na pasta do projeto:

```bash
cd C:\Users\Leandro\mmpoly

# Inicia o repositório Git (só na primeira vez)
git init
git add .
git commit -m "primeiro commit"

# Cria repo no GitHub e conecta (substitua SEU_USUARIO)
git remote add origin https://github.com/SEU_USUARIO/gababot.git
git branch -M main
git push -u origin main
```

> O `.gitignore` já está configurado para **nunca enviar** o `.env` com suas chaves.

---

### Passo B — Clonar na VPS e instalar

Conecte na VPS via SSH e rode:

```bash
ssh user@SEU_IP_VPS

# Clona o projeto
git clone https://github.com/SEU_USUARIO/gababot.git mmpoly
cd mmpoly

# Instala tudo
bash install.sh
```

Pronto. O instalador faz o resto automaticamente.

---

### Passo C — Atualizar o bot (quando fizer mudanças)

No seu PC, commita e sobe as mudanças:

```bash
cd C:\Users\Leandro\mmpoly
git add .
git commit -m "ajuste no grid"
git push
```

Na VPS, baixa e reinicia:

```bash
cd ~/mmpoly
git pull

# Copia arquivos atualizados para o bot em produção
sudo cp -r bot core data execution risk config /opt/gababot/
sudo chown -R botquant:botquant /opt/gababot/
sudo systemctl restart botquant
```

---

## Deploy alternativo — SCP (sem GitHub)

Se preferir não usar GitHub, envie direto por SCP:

```bash
# No seu PC (Git Bash ou WSL)
cd C:\Users\Leandro
tar -czf gababot.tar.gz mmpoly/
scp gababot.tar.gz user@SEU_IP_VPS:~

# Na VPS
ssh user@SEU_IP_VPS
tar -xzf gababot.tar.gz
cd mmpoly
bash install.sh
```

---

## 2. Rodar o instalador

```bash
bash install.sh
```

O script faz automaticamente:
- Instala Python 3.11, git, build-essential
- Cria usuário `botquant` (sem shell, seguro)
- Copia arquivos para `/opt/gababot/`
- Cria virtualenv e instala todas as dependências
- Configura o serviço `botquant` no systemd
- Teste rápido de imports

> Leva ~2–3 minutos dependendo da VPS.

---

## 3. Configurar credenciais

```bash
sudo nano /opt/gababot/.env
```

Preencha com suas credenciais Polymarket:

```env
POLY_WALLET_TYPE=magic
POLY_PRIVATE_KEY=0x...sua_chave_privada...
POLY_FUNDER=0x...seu_endereco_funder...
```

### Como pegar a POLY_PRIVATE_KEY (Magic Link)

1. Acesse [polymarket.com](https://polymarket.com) e faça login
2. Abra o DevTools do navegador (`F12`)
3. Vá em **Application** → **Local Storage** → `https://polymarket.com`
4. Procure pela chave `"openlogin_store"` ou `"magic_auth"`
5. Copie o valor do campo `privateKey`

### Como pegar o POLY_FUNDER

1. No Polymarket, clique no seu perfil
2. O endereço exibido (começa com `0x`) é o seu funder address

```bash
# Proteger o arquivo de credenciais
sudo chmod 600 /opt/gababot/.env
sudo chown botquant:botquant /opt/gababot/.env
```

---

## 4. Testar em dry-run

Antes de colocar dinheiro real, sempre teste em modo simulação:

```bash
# Garante que dry_run: true está no config
grep dry_run /opt/gababot/config/bot.yaml
# Deve mostrar: dry_run: true

# Roda o bot direto (Ctrl+C para parar)
cd /opt/gababot
sudo -u botquant venv/bin/python -m bot.main
```

O bot vai:
- Descobrir mercados BTC 15min via Gamma API
- Exibir intents de PLACE_ORDER no terminal (sem enviar à exchange)
- Mostrar o grid sendo calculado a cada tick

Exemplo de saída esperada:
```
[info] bot_starting dry_run=True mode=auto coins=['btc']
[info] market_added_auto name=btc-15m-... time_remaining=720s liquidity=$15,000
[info] state_change from_state=IDLE to_state=QUOTING regime=MID
[info] book_warmup market=btc-15m-... bid=0.52 ask=0.58
```

---

## 5. Ativar modo real

Quando estiver satisfeito com os logs do dry-run:

```bash
sudo nano /opt/gababot/config/bot.yaml
```

Mude a última linha:
```yaml
dry_run: false   # era true
```

---

## 6. Iniciar como serviço

```bash
# Inicia o bot
sudo systemctl start botquant

# Habilita auto-start na inicialização da VPS
sudo systemctl enable botquant

# Verifica status
sudo systemctl status botquant
```

---

## 7. Monitorar

### Logs em tempo real

```bash
# Log do systemd (tudo)
sudo journalctl -u botquant -f

# Só eventos do bot (JSON)
tail -f /opt/gababot/logs/events.jsonl | python3 -m json.tool

# Só fills (trades executados)
tail -f /opt/gababot/logs/trades.jsonl

# PnL snapshots a cada 30s
tail -f /opt/gababot/logs/pnl.jsonl
```

### Comandos de controle

```bash
sudo systemctl stop botquant      # para o bot
sudo systemctl restart botquant   # reinicia
sudo systemctl status botquant    # status atual
```

---

## 8. Atualizar o bot

Quando quiser atualizar após mudanças no código:

```bash
# No seu PC, gera novo tar
cd C:\Users\Leandro
tar -czf gababot.tar.gz mmpoly/
scp gababot.tar.gz user@SEU_IP_VPS:~

# Na VPS
sudo systemctl stop botquant
cd ~
tar -xzf gababot.tar.gz
sudo cp -r mmpoly/{bot,core,data,execution,risk,config} /opt/gababot/
sudo chown -R botquant:botquant /opt/gababot/
sudo systemctl start botquant
```

---

## Configuração do Grid (resumo)

Arquivo: `/opt/gababot/config/bot.yaml`

```yaml
grid:
  max_levels: 5           # 5 níveis por lado
  level_spacing_ticks: 2  # 2¢ entre níveis
  level_size: 5           # 5 shares por nível

min_spread: 0.04          # só entra se spread >= 4¢
net_soft_limit: 10        # começa a inclinar grid com 10 shares líquidos
net_hard_limit: 25        # para de comprar com 25 shares líquidos
max_daily_loss: -5.0      # kill switch com -$5 no dia
dry_run: false            # false = ordens reais
```

### Capital comprometido por janela (MID, neutro)

| Regime | Níveis ativos | Capital aprox. |
|--------|--------------|----------------|
| EARLY  | 1 BUY × 2 tokens | ~$5 |
| MID    | 5 BUY × 2 tokens | ~$25 |
| LATE   | 0 BUY, só SELL | — |

---

## Estrutura de arquivos

```
/opt/gababot/
├── bot/
│   ├── main.py          # Entry point
│   ├── supervisor.py    # Auto-restart watchdog
│   └── logger.py        # Logs JSON
├── core/
│   ├── engine.py        # State machine + cancel seletivo
│   ├── quoter.py        # Grid 5x5 com skew de inventário
│   ├── pair.py          # Detecção de par/arb
│   └── types.py         # Todos os dataclasses
├── data/
│   ├── book.py          # Cache do order book (WS)
│   ├── inventory.py     # Posição por mercado
│   └── fills.py         # Histórico de fills
├── execution/
│   ├── poly_client.py   # API Polymarket CLOB
│   ├── ws_feed.py       # WebSocket com auto-reconect
│   ├── order_manager.py # Lifecycle de ordens + índice grid
│   └── market_scanner.py# Auto-discovery via Gamma API
├── risk/
│   ├── manager.py       # Kill switch, rate limits, PnL floor
│   └── limits.py        # Validação de posição/net
├── config/
│   ├── bot.yaml         # Parâmetros do bot
│   └── markets.yaml     # Modo auto ou manual
├── services/
│   ├── botquant.service # Systemd unit
│   └── .env.example     # Template de credenciais
├── logs/                # Gerado automaticamente
│   ├── events.jsonl     # Todos os eventos
│   ├── trades.jsonl     # Fills executados
│   └── pnl.jsonl        # Snapshots de PnL
├── .env                 # Suas credenciais (nunca commitar)
├── requirements.txt
└── install.sh
```

---

## Problemas comuns

### Bot não encontra mercados
```
[warning] no_markets_found
```
→ O scanner busca mercados BTC 15min ativos. Se não houver janela aberta no momento, aguarda até 30s para a próxima.

### Erro de autenticação
```
[error] auth_error status=401
```
→ `POLY_PRIVATE_KEY` ou `POLY_FUNDER` incorretos. Reveja o `.env`.

### Kill switch acionado
```
[critical] kill_switch reason='daily_pnl=-5.00 < -5.0'
```
→ Perda diária atingiu o limite. Bot entra em cooldown de 30min automaticamente. Ajuste `max_daily_loss` no `bot.yaml` se necessário.

### Book stale (dados desatualizados)
```
[warning] book_stale market=...
```
→ WebSocket desconectou. O supervisor reinicia automaticamente em até 10s.
