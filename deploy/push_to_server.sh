#!/usr/bin/env bash
# Runs ON YOUR MAC. Moves the bot to the server: stops the local runner, copies the
# three private files the repo doesn't contain, then runs server_setup.sh remotely.
#
#   deploy/push_to_server.sh <server-ip>
set -euo pipefail
IP=${1:?usage: deploy/push_to_server.sh <server-public-ip>}
HOST="ubuntu@$IP"
cd "$(dirname "$0")/.."
KEY_PATH=$(grep '^KALSHI_PRIVATE_KEY_PATH=' .env | cut -d= -f2- | sed "s|^~|$HOME|")

echo "== checking SSH to $HOST"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$HOST" 'echo ok: $(hostname), $(uname -m)'

echo "== stopping the local runner (one runner per paper account)"
pkill -f "scripts/paper_run.py" || true
sleep 2

echo "== copying .env, API key and paper account"
ssh "$HOST" 'mkdir -p ~/.kalshi ~/kalshi-perps/data/runner && chmod 700 ~/.kalshi'
scp -q "$KEY_PATH" "$HOST:~/.kalshi/demo-key.pem"
scp -q .env "$HOST:~/kalshi-perps/.env.incoming"
[ -f data/runner/paper_state.json ] && scp -q data/runner/paper_state.json "$HOST:~/kalshi-perps/data/runner/paper_state.json"
# Point the key path at the server's home directory.
ssh "$HOST" 'sed "s|^KALSHI_PRIVATE_KEY_PATH=.*|KALSHI_PRIVATE_KEY_PATH=~/.kalshi/demo-key.pem|" ~/kalshi-perps/.env.incoming > /tmp/.env.kp && rm ~/kalshi-perps/.env.incoming && chmod 600 ~/.kalshi/demo-key.pem'

echo "== running setup on the server"
ssh "$HOST" 'set -e; APP=~/kalshi-perps
  if [ ! -d $APP/.git ]; then
    sudo apt-get update -qq && sudo apt-get install -y -qq git > /dev/null
    tmp=$(mktemp -d); git clone -q https://github.com/AnanmayS/kalshi-perps.git $tmp/k
    cp -r $tmp/k/. $APP/ && rm -rf $tmp
  fi
  mv /tmp/.env.kp $APP/.env && chmod 600 $APP/.env
  bash $APP/deploy/server_setup.sh'

cat <<MSG

Done. The runner and dashboard now run on the server and restart on reboot.
  Dashboard:  ssh -N -L 8765:localhost:8765 $HOST   then open http://localhost:8765
  Live log:   ssh $HOST 'tail -f ~/kalshi-perps/data/runner/runner.log'
MSG
