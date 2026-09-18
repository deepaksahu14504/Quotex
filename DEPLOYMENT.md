# QuotexAutoTrader — Ubuntu Production Deployment

One critical architectural constraint before anything else:

> **Run exactly ONE backend process. Never `--workers N` with N>1, never multiple
> systemd instances behind a load balancer.** Per-user state (Orchestrator
> registry, WebSocket hub, Telegram pollers, indicator cache) lives in that
> process's memory — a second worker would have its own empty registry and
> users would randomly land on either one. This constraint is about that
> in-memory state, not the database: the backend now talks to PostgreSQL
> (see "Database" below), which handles concurrent writers natively, so the
> old SQLite single-writer caveat no longer applies — but running multiple
> backend processes still requires moving the in-memory registry/hub state
> to something shared (Redis), which is a real architecture change, not a
> flag flip — flag it to me when you get there.

---

## 1. Prerequisites

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git nginx ufw certbot python3-certbot-nginx build-essential postgresql-16 libpq-dev
```

Node 20 (for the one-time frontend build — not needed at runtime):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node -v   # v20.x
```

Create a dedicated non-root user to run the service:

```bash
sudo useradd -m -s /bin/bash qat
sudo su - qat
```

Everything below runs as the `qat` user unless noted.

---

## 2. Get the code onto the server

```bash
cd ~
# upload the zip via scp, or git clone your repo
unzip QuotexAutoTrader-phase7-livestream.zip
mv QuotexAutoTrader app
cd app
```

## 3. Database (PostgreSQL)

Create the role and database the backend will connect to:

```bash
sudo -u postgres psql -c "CREATE ROLE qat_app WITH LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';"
sudo -u postgres psql -c "CREATE DATABASE qat OWNER qat_app;"
```

Put the same credentials in `backend/.env` as `DATABASE_URL` (step 4 below):

```
DATABASE_URL=postgresql+psycopg://qat_app:CHANGE_ME_STRONG_PASSWORD@localhost:5432/qat
```

Migrating from an earlier SQLite deployment? Schema is created automatically
by Alembic the first time the backend starts (see step 6). To carry over
existing accounts/broker-sessions/backtest history from `backend/data/app.db`,
run the one-off data migration **after** the first successful start (so the
tables already exist) and **before** pointing users at the new deployment:

```bash
cd ~/app/backend && source .venv/bin/activate
python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db
```

It's safe to re-run (existing rows are skipped, nothing is duplicated) and
it never modifies the source `app.db`, so there's no destructive step to
worry about if something looks off — just fix it and run it again.

---

## 4. Backend setup

```bash
cd ~/app/backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# Only if you're using the real Quotex provider (you are):
pip install -r requirements-quotex.txt 2>/dev/null || true
```

Configure `.env`:

```bash
cp .env.example .env
nano .env
```

Set for production:

```ini
MARKET_PROVIDER=paper          # legacy fallback only — real per-user Quotex
                                # credentials go through the app UI now, not .env
QUOTEX_IS_DEMO=true             # global fallback default; per-user overrides via UI

HOST=127.0.0.1                  # bind to localhost only — Nginx does the public side
PORT=8090
CORS_ORIGINS=https://your-domain.com
```

Leave `JWT_SECRET` / `SESSION_ENCRYPTION_KEY` blank — the backend generates
and writes them into `.env` itself on first start. **From that moment,
`.env` and the PostgreSQL `qat` database together are your account database
and encrypted broker-session store. Back both up (see "Backups" below). If
you lose or rotate `SESSION_ENCRYPTION_KEY`, every stored Quotex session
becomes undecryptable and every user has to re-enter their credentials.**

## 5. Frontend build

The backend serves the built frontend directly (`frontend/dist`) — no
separate Node process runs in production.

```bash
cd ~/app/frontend
npm ci
npm run build
```

Confirm `~/app/frontend/dist/index.html` exists. Re-run this after any
frontend change; the backend picks it up on next restart (or immediately —
it's served as static files, no restart needed for frontend-only changes).

## 6. First manual run (sanity check before wiring systemd)

```bash
cd ~/app/backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

In another shell:

```bash
curl -s http://127.0.0.1:8090/api/health
# {"ok":true,"version":"2.0.0"}
```

Ctrl+C once confirmed. Check `~/app/backend/.env` — you should now see
`JWT_SECRET=` and `SESSION_ENCRYPTION_KEY=` populated. Back this file up now.

---

## 7. systemd service

Exit back to your sudo user (`exit` from the `qat` shell) and create:

`/etc/systemd/system/quotexautotrader.service`

```ini
[Unit]
Description=QuotexAutoTrader backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qat
Group=qat
WorkingDirectory=/home/qat/app/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/qat/app/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
Restart=always
RestartSec=3

# Give in-flight trades/orders a chance to be recorded before SIGKILL.
TimeoutStopSec=20

# Basic hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/qat/app/backend/data /home/qat/app/backend/.env /home/qat/app/backend/sessions

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now quotexautotrader
sudo systemctl status quotexautotrader
journalctl -u quotexautotrader -f    # tail logs
```

`Restart=always` is your automatic-recovery net for process crashes; the
app's own WebSocket/broker reconnect logic (Phase 2) handles connection
drops without needing a process restart at all.

---

## 8. Nginx reverse proxy (TLS + WebSocket)

`/etc/nginx/sites-available/quotexautotrader`

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8090/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        # Must exceed the app's heartbeat timeout (45s) or Nginx will kill
        # idle-looking WebSocket connections out from under the client.
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/quotexautotrader /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

TLS:

```bash
sudo certbot --nginx -d your-domain.com
```

Certbot edits the config to redirect 80→443 and auto-renews via its own
systemd timer — nothing further needed.

## 9. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'   # 80 + 443
sudo ufw enable
sudo ufw status
```

Port 8090 is never exposed — only reachable via Nginx on localhost.

---

## 10. Your validation checklist, mapped to how to actually run each one

**Session restore after backend restart**
```bash
sudo systemctl restart quotexautotrader
journalctl -u quotexautotrader -n 50
```
Watch for the restore-on-startup log lines (users with a saved broker
session get auto-reconnected in the background — see `lifespan()` in
`main.py`). Confirm in the UI that you weren't asked to log in again.

**Reconnect after network interruption** — simulate a drop without
restarting the process:
```bash
# On the server, block outbound to Quotex briefly, then restore:
sudo iptables -A OUTPUT -d qxbroker.com -j DROP   # adjust to actual resolved IP if needed
sleep 30
sudo iptables -D OUTPUT -d qxbroker.com -j DROP
```
Watch the backoff/reconnect notices in the UI and in `journalctl -f`.

**24–48h soak test**
```bash
# Lightweight uptime/health poll, log to file:
while true; do
  echo "$(date -Iseconds) $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/api/health)"
  sleep 60
done >> ~/soak-health.log &
```
Also watch resident memory over time for the one process (a slow, steady
climb over 48h suggests a leak — the indicator cache is bounded per-key
(TAIL_ROWS=40), but worth confirming under real load):
```bash
watch -n 60 'ps -o pid,rss,vsz,etime,cmd -p $(pgrep -f "uvicorn app.main")'
```

**Multi-user concurrency / CPU / memory** — register a few test accounts,
run them concurrently in demo mode, then:
```bash
top -p $(pgrep -f "uvicorn app.main")
```
Since it's single-process/single-core-bound by the GIL for CPU-bound work
(the indicator math is the only real CPU cost — I/O is all async), if you
see sustained high CPU with many concurrent users, that's the signal you've
outgrown one process and need the sharding conversation, not a config
tweak.

**Signal accuracy / trade execution** — no shortcut here; demo-account
trading over a real multi-hour session is the only real test. Cross-check
`/api/trades` history against Quotex's own account history for the same
window.

---

## 11. Backups

Three things matter: `backend/.env` (secrets), the PostgreSQL `qat`
database (accounts, broker sessions, backtest history), and `backend/data/`
(per-user trade history + settings files that never moved into the
database — see the migration report for what's actually in there).

```bash
crontab -e
```
```cron
0 * * * * pg_dump -U qat_app -h localhost qat | gzip > /home/qat/backups/qat-db-$(date +\%Y\%m\%d\%H\%M).sql.gz && find /home/qat/backups -name 'qat-db-*' -mtime +7 -delete
5 * * * * tar -czf /home/qat/backups/qat-data-$(date +\%Y\%m\%d\%H\%M).tar.gz -C /home/qat/app/backend data .env && find /home/qat/backups -name 'qat-data-*' -mtime +7 -delete
```

Restore with `gunzip -c qat-db-<ts>.sql.gz | psql -U qat_app -h localhost qat`.

---

## 12. Updating to a new version later

```bash
sudo systemctl stop quotexautotrader
cd ~/app
# replace source files (keep backend/.env and backend/data/ — don't overwrite these)
cd backend && source .venv/bin/activate && pip install -r requirements.txt
cd ../frontend && npm ci && npm run build
sudo systemctl start quotexautotrader
```
