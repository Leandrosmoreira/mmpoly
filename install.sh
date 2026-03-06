#!/bin/bash
# ============================================
# GabaBook MM Bot — VPS Install Script
# ============================================
# Usage: bash install.sh
# Tested on: Ubuntu 22.04 / Debian 12
# ============================================

set -e

BOT_DIR="/opt/gababot"
BOT_USER="botquant"
PYTHON_VERSION="python3.11"

echo "=========================================="
echo " GabaBook MM Bot — VPS Installer"
echo "=========================================="

# === 1. System dependencies ===
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
# Prefer python3.11 if in repos; otherwise use system python3
if sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev 2>/dev/null; then
    PYTHON_VERSION="python3.11"
else
    echo "  python3.11 not in repos, using system python3..."
    PYTHON_VERSION="python3"
    sudo apt-get install -y -qq python3 python3-venv python3-dev
fi
sudo apt-get install -y -qq \
    python3-pip \
    build-essential \
    git \
    curl \
    jq

echo "  Using: $($PYTHON_VERSION --version)"

# === 2. Create bot user (if not exists) ===
echo "[2/7] Setting up bot user..."
if ! id "$BOT_USER" &>/dev/null; then
    sudo useradd -r -m -s /bin/bash "$BOT_USER"
    echo "  Created user: $BOT_USER"
else
    echo "  User $BOT_USER already exists"
fi

# === 3. Create bot directory ===
echo "[3/7] Setting up bot directory..."
sudo mkdir -p "$BOT_DIR"
sudo chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# Copy project files
echo "  Copying project files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sudo cp -r "$SCRIPT_DIR"/{bot,core,data,execution,risk,config,services,requirements.txt} "$BOT_DIR/"
sudo mkdir -p "$BOT_DIR/logs"
sudo chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# === 4. Python virtualenv + dependencies ===
echo "[4/7] Creating virtual environment..."
sudo -u "$BOT_USER" $PYTHON_VERSION -m venv "$BOT_DIR/venv"
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/pip" install --upgrade pip -q
echo "  Installing dependencies..."
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

echo "  Installed packages:"
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/pip" list --format=columns | grep -E "py-clob|py-order|aiohttp|structlog|web3|eth-account|dotenv"

# === 5. Setup .env ===
echo "[5/7] Setting up environment..."
if [ ! -f "$BOT_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/.env" ]; then
        sudo cp "$SCRIPT_DIR/.env" "$BOT_DIR/.env"
        echo "  Copied .env from source"
    else
        sudo cp "$BOT_DIR/services/.env.example" "$BOT_DIR/.env"
        echo "  Created .env from template — EDIT IT with your credentials!"
        echo "  >>> sudo nano $BOT_DIR/.env <<<"
    fi
    sudo chown "$BOT_USER:$BOT_USER" "$BOT_DIR/.env"
    sudo chmod 600 "$BOT_DIR/.env"
else
    echo "  .env already exists, keeping it"
fi

# === 6. Install systemd service ===
echo "[6/7] Installing systemd service..."
sudo cp "$BOT_DIR/services/botquant.service" /etc/systemd/system/botquant.service

# Update paths in service file
sudo sed -i "s|EnvironmentFile=.*|EnvironmentFile=$BOT_DIR/.env|g" /etc/systemd/system/botquant.service
sudo sed -i "s|WorkingDirectory=.*|WorkingDirectory=$BOT_DIR|g" /etc/systemd/system/botquant.service
sudo sed -i "s|ExecStart=.*|ExecStart=$BOT_DIR/venv/bin/python -m bot.supervisor|g" /etc/systemd/system/botquant.service
sudo sed -i "s|ReadWritePaths=.*|ReadWritePaths=$BOT_DIR/logs|g" /etc/systemd/system/botquant.service

sudo systemctl daemon-reload
echo "  Service installed: botquant.service"

# === 7. Verify ===
echo "[7/7] Verifying installation..."
echo ""
echo "  Bot directory: $BOT_DIR"
echo "  Python: $($BOT_DIR/venv/bin/python --version)"
echo "  Venv: $BOT_DIR/venv/"
echo ""

# Quick import test
sudo -u "$BOT_USER" "$BOT_DIR/venv/bin/python" -c "
import sys
sys.path.insert(0, '$BOT_DIR')
from core.types import BotConfig, BotState
from core.engine import Engine
from execution.poly_client import PolyClient
print('  Import test: OK')
print('  Bot states:', [s.value for s in BotState])
" 2>&1 || echo "  Import test: FAILED (check dependencies)"

echo ""
echo "=========================================="
echo " Installation complete!"
echo "=========================================="
echo ""
echo " Next steps:"
echo ""
echo " 1. Edit credentials:"
echo "    sudo nano $BOT_DIR/.env"
echo ""
echo " 2. Edit markets (add real token IDs):"
echo "    sudo nano $BOT_DIR/config/markets.yaml"
echo ""
echo " 3. Test dry-run:"
echo "    cd $BOT_DIR && sudo -u $BOT_USER venv/bin/python -m bot.main"
echo ""
echo " 4. Start as service:"
echo "    sudo systemctl start botquant"
echo "    sudo systemctl enable botquant  # auto-start on boot"
echo ""
echo " 5. Monitor:"
echo "    sudo journalctl -u botquant -f"
echo "    tail -f $BOT_DIR/logs/events.jsonl"
echo ""
echo "=========================================="
