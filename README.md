# QuotexAutoTrader

A fast, beautiful, **glassmorphic automated-trading cockpit** for the Quotex
platform, built on top of the [PyQuotex](https://github.com/cleitonleonel/pyquotex)
async client.

It scans markets with a multi-strategy **confluence engine**, surfaces
high-probability signals, and can **auto-execute** or wait for your **one-tap /
Telegram approval** — all behind a strict risk manager. It runs out of the box
in a **safe simulated market** (no broker, no credentials) and switches to a
real Quotex **demo** or **live** account when you add credentials.

> ⚠️ **Risk disclaimer.** Binary options are high-risk and restricted/banned in
> many jurisdictions. This is an unofficial tool. It is **demo-first** by
> design. Validate everything on demo before risking real money. Trading is at
> your own risk.

---

## ✨ Features

- **Confluence engine** — Trend-Pullback, Mean-Reversion, Momentum-Breakout and
  MACD-Cross strategies vote per candle; signals fire only above your confidence
  threshold and minimum payout.
- **Risk manager (always on)** — stake sizing (fixed/%), daily loss limit,
  max consecutive losses → auto-pause, max trades/day, cooldown, optional capped
  martingale, global kill-switch.
- **Three modes** — `Auto` (hands-off), `Manual` (approve each signal), `Off`.
- **Live UI** — glassmorphism, aurora + animated fog, candlestick chart,
  confidence rings, equity curve, trade history. Mobile-first PWA-style layout.
- **Telegram** — signal alerts + result summaries, and inbound commands
  (`/status`, `/enter`, `/skip`, `/pause`, `/resume`, `/stop`, `/balance`).
- **Notifications & sound** — in-app toasts, browser notifications, WebAudio SFX.
- **Provider abstraction** — `simulated` (default) or `pyquotex` (real broker).

---

## 🧱 Architecture

```
frontend/  React + Vite + TS + Tailwind v4 + Framer Motion + lightweight-charts
backend/   FastAPI (async) — engine, risk, orchestrator, providers, telegram, WS
vendor/    pyquotex (the upstream Quotex client, used by the pyquotex provider)
```

Data flow: `Provider → Confluence Engine → Risk Gate → Orchestrator → (auto/manual) → Order → Result → WS broadcast → UI / Telegram`.

---

## 🚀 Quick start

Cross-platform (Linux, macOS, Windows). Requires **Python 3.11+** and **Node 18+**.

### Linux / macOS

```bash
# one-liners (create venv, install, run)
bash run-backend.sh        # API + built UI on http://127.0.0.1:8090
bash run-dev.sh            # Vite dev server on http://localhost:5173 (hot reload)
```

Or manually:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # defaults to the safe simulated provider
python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

```bash
cd frontend
npm install
npm run dev                 # http://localhost:5173  (proxies /api and /ws to :8090)
```

### Windows (PowerShell)

```powershell
./run-backend.ps1
./run-dev.ps1
```

Or manually:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

### Run as one server (production, any OS)

```bash
cd frontend && npm run build      # outputs frontend/dist
# the backend then serves the built app at http://127.0.0.1:8090
```

---

## 🔌 Connecting a real Quotex account

By default the app uses the **simulated** market (safe, no login). To trade on a
real account, switch to the **PyQuotex** provider in **Settings → Data provider**.

Install the provider deps (Linux/macOS shown; on Windows use `python` and
`.\.venv\Scripts\activate`):

```bash
cd backend
source .venv/bin/activate
pip install -r requirements-pyquotex.txt
```

Then edit `backend/.env`:

```
MARKET_PROVIDER=pyquotex
QUOTEX_EMAIL=you@example.com
QUOTEX_PASSWORD=your_password
QUOTEX_IS_DEMO=true          # keep true until you've validated on demo
```

PyQuotex manages its own session/login (and works well on headless servers).

---

## 📲 Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) → get the **token**.
2. Get your **chat ID** (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. In the app → **Settings → Telegram**: enable, paste token + chat ID, choose
   what to send and whether to allow commands. Only your chat ID can command the
   bot.

---

## ⚙️ Configuration reference

- **Env (`backend/.env`)**: provider choice, broker credentials, Telegram
  fallback, host/port, CORS.
- **Runtime (UI → Settings)**: persisted to `backend/data/runtime_settings.json`
  (trading mode, risk, strategies, UX, Telegram).
- **History**: `backend/data/trades.json`.

---

## 🗺️ Roadmap

- Backtesting report UI on historical candles
- Per-strategy performance attribution
- Multi-asset concurrent positions
- SQLite/Postgres storage backend (interface already abstracted)

---

## License

MIT. Provided as-is, with no warranty.
