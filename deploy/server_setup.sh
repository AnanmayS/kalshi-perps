#!/usr/bin/env bash
# Runs ON THE SERVER (Ubuntu 22.04/24.04, any user with sudo). Safe to re-run.
# Installs Python deps, clones/updates the repo, installs and starts the systemd services.
set -euo pipefail
REPO=https://github.com/AnanmayS/kalshi-perps.git
APP=$HOME/kalshi-perps
USER_NAME=$(whoami)

sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pip > /dev/null
sudo timedatectl set-timezone UTC || true

# Small free-tier VMs (e.g. Google's e2-micro, 1 GB RAM) get a 1 GB swap file as headroom.
if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 1500000 ] && ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
fi

if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only
else
  git clone -q "$REPO" "$APP"
fi
cd "$APP"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/runner
chmod 700 ~/.kalshi 2>/dev/null || true
chmod 600 ~/.kalshi/*.pem .env 2>/dev/null || true

if [ ! -f .env ]; then
  echo "!! $APP/.env is missing: run deploy/push_to_server.sh from your Mac first" >&2
  exit 1
fi

echo "== offline tests"
.venv/bin/python -m pytest -q | tail -1
echo "== connectivity check"
.venv/bin/python scripts/market_check.py | tail -8

# Install the units for whichever user runs this (ubuntu on Oracle, your chosen name on Google Cloud).
for unit in kalshi-runner kalshi-dashboard; do
  sed -e "s|^User=ubuntu|User=$USER_NAME|" -e "s|/home/ubuntu|$HOME|g" "deploy/$unit.service" \
    | sudo tee "/etc/systemd/system/$unit.service" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-runner kalshi-dashboard
sudo systemctl restart kalshi-runner kalshi-dashboard
sleep 5
systemctl --no-pager --lines=0 status kalshi-runner kalshi-dashboard | grep -E "●|Active:"
echo "== runner log"
tail -5 data/runner/runner.log
