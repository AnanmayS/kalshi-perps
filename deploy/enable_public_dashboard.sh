#!/usr/bin/env bash
# Runs ON THE SERVER. Publishes the read-only dashboard at https://<ip-with-dashes>.sslip.io
# using Caddy (automatic Let's Encrypt HTTPS). The private dashboard (port 8765, SSH tunnel)
# is untouched. Needs inbound TCP 80 and 443 allowed in the cloud firewall. Safe to re-run.
set -euo pipefail
APP=$HOME/kalshi-perps
USER_NAME=$(whoami)
IP=$(curl -s --max-time 10 https://ifconfig.me)
HOST="${PUBLIC_HOST:-${IP//./-}.sslip.io}"

sudo apt-get install -y -qq caddy > /dev/null

sed -e "s|^User=ubuntu|User=$USER_NAME|" -e "s|/home/ubuntu|$HOME|g" "$APP/deploy/kalshi-dashboard-public.service" \
  | sudo tee /etc/systemd/system/kalshi-dashboard-public.service > /dev/null

sudo tee /etc/caddy/Caddyfile > /dev/null <<CADDY
$HOST {
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}
	# Read-only instance only; the private dashboard on :8765 is never proxied.
	reverse_proxy 127.0.0.1:8766
}
CADDY

sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-dashboard-public
sudo systemctl restart kalshi-dashboard-public
sudo systemctl enable caddy
sudo systemctl restart caddy
sleep 3
systemctl --no-pager --lines=0 status kalshi-dashboard-public caddy | grep -E "●|Active:"
echo "Public dashboard: https://$HOST"
echo "(Caddy needs ports 80 and 443 open to get its certificate; check: sudo journalctl -u caddy -n 30)"
