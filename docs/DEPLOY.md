# Run it 24/7 on a free cloud server

The paper runner and dashboard run on a small free-tier Linux server as `systemd` services: they start on boot and restart if they crash. Your Mac can be closed or off.

Two free options are covered: **Oracle Cloud Always Free** (below) and **[Google Cloud's free e2-micro](#google-cloud-free-tier)**. Free-tier terms and console layouts change from time to time; if a screen looks different, the names should still be close.

## 1. Create the Oracle account (you)

1. Go to <https://www.oracle.com/cloud/free/> and sign up. A card is required for identity verification; Always Free resources aren't charged.
2. Pick a **home region** carefully: it can't be changed later, and free ARM capacity is easier to get in less busy regions.

## 2. Create the server (you)

In the Oracle console: **Compute → Instances → Create instance**.

| Setting | Value |
| --- | --- |
| Name | `kalshi-perps` |
| Image | **Canonical Ubuntu 24.04** (Change image → Ubuntu) |
| Shape | **Ampere `VM.Standard.A1.Flex`**, 1 OCPU, 6 GB memory (marked "Always Free-eligible"). If it says *out of capacity*, try another availability domain, or use `VM.Standard.E2.1.Micro` (also free). |
| Networking | Create a new virtual cloud network + public subnet; **Assign a public IPv4 address: yes** |
| SSH keys | **Paste public key**, the contents of `~/.ssh/id_ed25519.pub` on your Mac (`pbcopy < ~/.ssh/id_ed25519.pub` copies it) |

Click **Create**, wait until the instance is *Running*, and copy its **Public IP address**. No firewall changes are needed: SSH (port 22) is open by default, and the dashboard is only reachable through SSH.

## 3. Move the bot to the server (one command on your Mac)

```bash
cd ~/Desktop/kalshi-perps
deploy/push_to_server.sh <PUBLIC_IP>
```

This:

1. checks it can SSH in,
2. **stops the runner on your Mac** (one runner per paper account),
3. copies the three private files that aren't in the repo: `.env`, your demo API key, and `data/runner/paper_state.json` (so the open position and P&L carry over),
4. clones the repo on the server, installs dependencies, runs the offline tests and a Kalshi connectivity check, and starts both services.

Funding that comes due during the move is settled when the server's runner starts.

## 4. Watch it

```bash
# Dashboard: keep this running, then open http://localhost:8765 on your Mac
ssh -N -L 8765:localhost:8765 ubuntu@<PUBLIC_IP>       # Google Cloud: kalshi@<EXTERNAL_IP>

# Live runner log
ssh ubuntu@<PUBLIC_IP> 'tail -f ~/kalshi-perps/data/runner/runner.log'

# Service status
ssh ubuntu@<PUBLIC_IP> 'systemctl status kalshi-runner kalshi-dashboard --no-pager'
```

If a dashboard is also running on your Mac, stop it first (`pkill -f dashboard/server.py`) so the tunnel can use port 8765.

## Change strategy or size

Edit `ExecStart` in `/etc/systemd/system/kalshi-runner.service` on the server (e.g. `--size 1200`), then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart kalshi-runner
```

## Update the code

```bash
ssh ubuntu@<PUBLIC_IP> 'bash ~/kalshi-perps/deploy/server_setup.sh'   # git pull, reinstall, restart
```

## Stop it

```bash
ssh ubuntu@<PUBLIC_IP> 'sudo systemctl disable --now kalshi-runner kalshi-dashboard'
```

## Google Cloud free tier

Google's free tier includes one `e2-micro` VM (1 GB RAM, shared CPU) per month in certain US regions, plus 30 GB of standard disk. It's small but enough; the setup script adds a 1 GB swap file for headroom.

1. **Account.** Sign up at <https://console.cloud.google.com/> (card required for verification). Create a project, then open **Compute Engine** and click **Enable** for the API.
2. **Create the VM.** **Compute Engine → VM instances → Create instance**:

   | Setting | Value |
   | --- | --- |
   | Name | `kalshi-perps` |
   | Region | **`us-central1` (Iowa), `us-west1` (Oregon) or `us-east1` (South Carolina)**; the free tier only covers these |
   | Machine | Series **E2**, type **`e2-micro`** |
   | Boot disk | **Ubuntu 24.04 LTS** (x86/64), disk type **Standard persistent disk**, 30 GB (the "balanced" default isn't free) |
   | Firewall | leave HTTP/HTTPS unchecked |
   | SSH key | **Security → Manage access → Add item**, paste your public key ending in ` kalshi` (see below). Google creates a user named `kalshi`. |

   The public key line must end with the username. On your Mac:

   ```bash
   echo "$(cut -d' ' -f1,2 ~/.ssh/id_ed25519.pub) kalshi" | pbcopy
   ```

3. **Create**, wait for the green check, and copy the **External IP**.
4. **Move the bot** (note the user argument):

   ```bash
   deploy/push_to_server.sh <EXTERNAL_IP> kalshi
   ```

5. **Watch it** as in step 4 above, with `kalshi@<EXTERNAL_IP>` instead of `ubuntu@<PUBLIC_IP>`.

The external IP is ephemeral by default: it can change if you stop and start the VM (just use the new one). Google's pricing for external IPv4 addresses has changed over time, so check the billing page after the first few days and set a budget alert; a small charge there is the most likely surprise.

## Public read-only dashboard (optional)

To view the dashboard from any browser without an SSH tunnel, publish a **read-only** copy over HTTPS:

1. Allow inbound HTTP and HTTPS: Google Cloud → VM → **Edit → Firewalls → Allow HTTP traffic + Allow HTTPS traffic → Save** (Oracle: add ingress rules for TCP 80 and 443 to the subnet's security list).
2. On the server: `bash ~/kalshi-perps/deploy/enable_public_dashboard.sh`

It prints the URL, e.g. `https://34-75-82-205.sslip.io` (sslip.io maps the IP to a hostname, so Caddy can get a free Let's Encrypt certificate; no domain needed).

The public instance runs `dashboard/server.py --public` on port 8766: it shows the market, chart, book, funding and the strategy runner, has no controls and no manual paper account, answers every POST with 403, and polls every 10–15 s (pausing in background tabs) to stay within the free tier's outbound data. The private dashboard on 8765 is never proxied; keep using the SSH tunnel for it. If the VM's external IP changes (ephemeral IPs can change on stop/start), re-run the script to get the new URL and certificate.

## Things to know

- **Oracle idle reclamation.** Oracle may reclaim Always Free instances that stay mostly idle for a week, and this bot uses very little CPU. Upgrading the account to Pay As You Go (Always Free resources stay free) is Oracle's documented way to avoid that. Check the current policy when you sign up.
- **Google billing.** New accounts get a free trial credit; the e2-micro stays free after it ends as long as it's in one of the three regions above with a standard disk. Setting a budget alert in Billing is a cheap safety net.
- **Security.** The dashboard binds to `127.0.0.1` only: it has no login and can place paper orders, so never open port 8765 in Oracle's firewall. Use a demo key on the server, never a production key.
- **Nothing here sends orders to Kalshi.** The runner is paper-only regardless of `.env`.
