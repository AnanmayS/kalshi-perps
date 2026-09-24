#!/usr/bin/env bash
# Runs ON THE SERVER (Ubuntu 22.04/24.04, user "ubuntu"). Safe to re-run.
# Installs Python deps, clones/updates the repo, installs and starts the systemd services.
set -euo pipefail
REPO=https://github.com/AnanmayS/kalshi-perps.git
APP=/home/ubuntu/kalshi-perps

sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pip > /dev/null
sudo timedatectl set-timezone UTC || true

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

sudo cp deploy/kalshi-runner.service deploy/kalshi-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-runner kalshi-dashboard
sudo systemctl restart kalshi-runner kalshi-dashboard
sleep 5
systemctl --no-pager --lines=0 status kalshi-runner kalshi-dashboard | grep -E "●|Active:"
echo "== runner log"
tail -5 data/runner/runner.log
