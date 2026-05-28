#!/bin/bash
# setup.sh — Run once on a fresh Ubuntu VM to install all dependencies.
# Usage: bash setup.sh

set -e
cd "$(dirname "$0")"

echo "=== [1/5] Swap (1GB — helps Chromium on 1GB RAM VM) ==="
if ! swapon --show | grep -q /swapfile; then
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "  Swap enabled (1GB)"
else
    echo "  Swap already exists, skipping"
fi

echo "=== [2/5] System packages ==="
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git unzip

echo "=== [3/5] Python virtual environment ==="
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet requests gspread google-auth google-genai openpyxl urllib3 playwright

echo "=== [4/5] Playwright + Chromium ==="
playwright install chromium
playwright install-deps chromium

echo "=== [5/5] Cron job (every 2h, 08:00–18:00, Mon–Fri) ==="
SCRIPT_DIR="$(pwd)"
CRON_LINE="0 8,10,12,14,16,18 * * 1-5 cd $SCRIPT_DIR && $SCRIPT_DIR/venv/bin/python run_monitor.py >> $SCRIPT_DIR/cron.log 2>&1"

# Add only if not already present
( crontab -l 2>/dev/null | grep -v "run_monitor.py" ; echo "$CRON_LINE" ) | crontab -

echo ""
echo "✓ Setup complete."
echo "  Script dir : $SCRIPT_DIR"
echo "  Cron       : $CRON_LINE"
echo ""
echo "Next: run a test with:"
echo "  source venv/bin/activate && python run_monitor.py"
