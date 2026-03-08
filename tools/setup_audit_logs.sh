#!/bin/bash
# ============================================
# Configura logs do bot para gravar em mmpoly/logs/ (auditoria)
# ============================================
# Uso: a partir da raiz do clone mmpoly (ex: cd ~/mmpoly && bash tools/setup_audit_logs.sh)
# Requer: bot já instalado em /opt/gababot (install.sh já rodado)
# ============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MMPOLY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOGS_DIR="$MMPOLY_ROOT/logs"
GABABOT_LOGS="/opt/gababot/logs"

if [ ! -d "$GABABOT_LOGS" ]; then
  echo "Erro: $GABABOT_LOGS não existe. Rode install.sh antes."
  exit 1
fi

echo "Logs do bot serão gravados em: $LOGS_DIR"
echo "  vps_events.jsonl  (todos os eventos)"
echo "  vps_trades.jsonl  (fills/trades)"
echo ""

# Para o bot para não ter ficheiros abertos
if systemctl is-active --quiet botquant 2>/dev/null; then
  echo "Parando botquant..."
  sudo systemctl stop botquant
  STOPPED=1
else
  STOPPED=0
fi

mkdir -p "$LOGS_DIR"

# Se já existem ficheiros reais em /opt/gababot/logs, copiar para mmpoly/logs
if [ -f "$GABABOT_LOGS/events.jsonl" ] && [ ! -L "$GABABOT_LOGS/events.jsonl" ]; then
  cp "$GABABOT_LOGS/events.jsonl" "$LOGS_DIR/vps_events.jsonl"
fi
if [ -f "$GABABOT_LOGS/trades.jsonl" ] && [ ! -L "$GABABOT_LOGS/trades.jsonl" ]; then
  cp "$GABABOT_LOGS/trades.jsonl" "$LOGS_DIR/vps_trades.jsonl"
fi

# Criar ficheiros vazios se não existirem (para o bot poder escrever)
touch "$LOGS_DIR/vps_events.jsonl" "$LOGS_DIR/vps_trades.jsonl"
sudo chown botquant:botquant "$LOGS_DIR/vps_events.jsonl" "$LOGS_DIR/vps_trades.jsonl"

# Remover ficheiros reais e criar symlinks
sudo rm -f "$GABABOT_LOGS/events.jsonl" "$GABABOT_LOGS/trades.jsonl"
sudo ln -sf "$LOGS_DIR/vps_events.jsonl" "$GABABOT_LOGS/events.jsonl"
sudo ln -sf "$LOGS_DIR/vps_trades.jsonl" "$GABABOT_LOGS/trades.jsonl"

echo "Symlinks criados:"
ls -la "$GABABOT_LOGS"/events.jsonl "$GABABOT_LOGS"/trades.jsonl

if [ "$STOPPED" = 1 ]; then
  echo "Reiniciando botquant..."
  sudo systemctl start botquant
fi

echo ""
echo "Pronto. O bot grava automaticamente em:"
echo "  $LOGS_DIR/vps_events.jsonl"
echo "  $LOGS_DIR/vps_trades.jsonl"
