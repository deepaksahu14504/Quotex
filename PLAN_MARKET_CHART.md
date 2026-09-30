# Market Chart — Inspection Findings & Implementation Plan

Inspection only. No code was changed to produce this document. Every claim
below was read out of the tree at `03fd795`.

---

## 1. What already exists (this is an upgrade, not a new build)

**A chart is already on the Home tab.** `App.tsx:159` mounts it inside a
`GlassCard` in the 12-column Home grid:

```
<div className="lg:col-span-3">   <Dashboard />
<GlassCard className="h-[46vh] lg:col-span-6 lg:h-full">
  <Chart timeframe={settings?.trading.timeframe ?? "1m"} />
<div className="hidden lg:col-span-3"> <Signals />
```

`frontend/src/components/Chart.tsx` is 83 lines and already does:

| Already present | Where |
| --- | --- |
| `lightweight-charts` v5 candlestick series | `Chart.tsx:2` |
| Asset selector from the **existing** asset list (`api.assets()`), not a hardcoded one | `Chart.tsx:14-18` |
| Payout shown per asset (`{a.name} · {a.payout}%`) | `Chart.tsx:71` |
| Historical candles via `api.candles(asset, tf, 150)` | `Chart.tsx:47` |
| Crosshair + price scale + time scale | `Chart.tsx:26-33` |
| `autoSize: true` (responsive) | `Chart.tsx:34` |
| Dark terminal styling | `Chart.tsx:26-31` |

So the library question is already answered: **reuse `lightweight-charts`**,
which is the TradingView library and is already a dependency
(`package.json`: `"lightweight-charts": "^5.2.0"`).

### Frontend stack

React 19.2 · TypeScript ~6.0 · Vite 8 · Tailwind 4 (`@tailwindcss/vite`) ·
zustand 5 (state) · framer-motion 12 · oxlint. Build is `tsc -b && vite build`,
so **typecheck and build are the same command**.

### Design tokens (must be used instead of hardcoded colors)

`index.css:12-15`:

```
--color-up: #2bdca0   --color-down: #ff5a76
--color-accent: #4ad6ff   --color-accent-2: #8a7bff
```

`charts.tsx` already consumes them (`var(--color-up)` etc.). **The existing
`Chart.tsx` does not** — it hardcodes `#22e0a1`, `#ff5d73`, `#7c5cff`, which are
*close to but not equal to* the tokens. Fixing that drift is in scope.

---

## 2. The candle data pipeline (verified, not assumed)

```
Quotex WS / HTTP  ->  PyQuotexProvider.get_candles()  ->  /api/candles  ->  Chart
```

- `GET /api/candles?asset&timeframe&count` (`main.py:677`) calls
  `o.provider.get_candles(asset, timeframe, count)` and returns
  `[c.model_dump() for c in candles]`.
- `Candle` (`schemas.py`): `timestamp: float`, `open/high/low/close: float`,
  `volume: float = 0.0`, plus `volume_source` = `"real"` | `"synthetic"`.
- **Timestamps are SECONDS everywhere.** `market.py:366/372/400/410` all do
  `row[0] / 1000`, converting the broker's ms to seconds. No mixing found.
- `volume` on the Quotex path is a **tick-count proxy**, explicitly flagged
  `volume_source="synthetic"`, and the model comment states indicators must
  treat it as neutral. The chart will therefore send `volume: null` for
  synthetic volume rather than implying real order-book volume.

### `CandleStore` is a cache, not the source of truth

`CandleStore` (`services/candle_store.py`, built at `orchestrator.py:302`)
exposes `get_range / get_latest_n / upsert_many / fetch_or_load /
returns_series / count / last_updated / prune_older_than`.

Callers of `fetch_or_load`: **only** `validation_service.py:233` and `:239`.

Callers of `provider.get_candles`: `main.py:188`, `main.py:677` (the chart),
and `orchestrator.py:2128, 2582, 2600, 2613, 3594, 3607, 4231` — the trading
pipeline itself.

**Conclusion: `provider.get_candles()` is already the single source of truth,
and the chart already sits on it.** The chart must *not* be rerouted through
`CandleStore`; doing that would create the second pipeline the brief forbids.

### Timeframes are already modelled

`market.py:31-33`:

```python
TIMEFRAMES = {"30s": 30, "1m": 60, "2m": 120, "3m": 180, "5m": 300,
              "10m": 600, "15m": 900, "30m": 1800, "1h": 3600,
              "4h": 14400, "1day": 86400}
```

Every timeframe the brief asks for is natively supported **except the label
`1D`** — the model uses `"1day"`. So the chart needs a display-label alias
(`1D` → `1day`), not a new timeframe model. `_TF_MIN` (`market.py:132`) shows
the Binance path has no native 2m (`2m → 3`), so aggregation stays a fallback
only.

**There is no existing candle resample/aggregation code anywhere** (grep for
`resample`/`aggregat` returns only strategy-vote aggregation and log
aggregation). Aggregation is therefore new code, used only when a provider
cannot serve a timeframe natively.

---

## 3. The gaps that actually need work

### 3.1 The chart polls. The brief forbids polling.

`Chart.tsx:56`: `const id = setInterval(load, 2500);`

Every 2.5s it re-fetches 150 candles and calls `series.setData(...)` — a full
series replacement, which is both the polling the brief rejects and the
"recreate the whole chart" it warns against.

### 3.2 No `candle_update` event exists

The backend broadcasts exactly these WS events (grepped from
`orchestrator.py`): `analytics_update, auth_event, error, latency, notice,
otp_cleared, otp_required, pipeline_snapshot, pipeline_trace_update,
shadow_result, shadow_trade, signal, state, trade_closed, trade_opened`.

**No candle event.** This is the main backend addition.

The hook point already exists: `provider.new_data_event`
(`market.py:46`, set at `:297`) is awaited by the scan loop at
`orchestrator.py:2103-2105` with a 3.0s timeout. Live candle emission attaches
there rather than opening any new connection.

### 3.3 Silent error swallowing

`Chart.tsx:51-53`: `} catch {` / `/* ignore */` / `}`. The brief requires explicit
"No market data" / "Asset unavailable" / "Connecting…" states and *never* fake
candles. Today a failure renders an empty chart with no explanation.

### 3.4 Everything else the brief lists as missing

No timeframe selector (it is a prop only) · no EMA 9/21/50 · no Bollinger
Bands · no indicator toggles · no signal markers · no auto-trade status · no
data validation · no current-price line/label · no reconnect resync or
dedupe · hardcoded colors.

---

## 4. Proposed implementation

### Backend (small, additive)

1. **`GET /api/chart/candles?asset&timeframe&limit`** — a thin adapter over the
   existing `provider.get_candles`. Returns the brief's envelope
   (`{asset, timeframe, candles[]}`) with `volume: null` when
   `volume_source == "synthetic"`, and rejects malformed candles server-side.
   `GET /api/candles` is left untouched so nothing existing changes.
   *No credentials or session material in the response* — it carries OHLC only.
2. **Timeframe aggregation helper** (`services/candle_aggregate.py`) — buckets
   a lower timeframe into a higher one: `bucket = ts // period * period`,
   open from the earliest tick in the bucket, close from the latest, high/low
   as max/min. **The in-progress bucket is marked, never emitted as closed.**
   Used only when the provider lacks a timeframe natively.
3. **`candle_update` WS event** — emitted off `new_data_event` for the
   asset/timeframe the user is watching, throttled (not per tick), carrying one
   candle in the normalized shape. Reuses the existing `/ws` connection and its
   token auth. No new socket, no new auth.

### Frontend (component split, as the brief asks)

```
components/chart/
  MarketChart.tsx        orchestration only; owns the chart instance
  ChartToolbar.tsx       asset + payout + timeframe + auto-trade status
  TimeframeSelector.tsx  horizontally scrollable on mobile
  IndicatorControls.tsx  EMA 9 / 21 / 50 / BB toggles
  ChartStatus.tsx        Connecting / Reconnecting / No data / Unavailable
  SignalMarkers.tsx      markers from EXISTING signal data only
  useChartCandles.ts     subscription + history/live merge + dedupe
  indicators.ts          EMA, Bollinger — pure functions
  candleMath.ts          normalization, validation, bucketing
  chartTheme.ts          reads the CSS design tokens
```

`Chart.tsx` becomes a thin wrapper so `App.tsx:159` keeps working.

**Types** (`types.ts`): `Candle`, `ChartTimeframe`, `ChartIndicator`,
`ChartSignal`.

**Rendering discipline:** historical load uses `setData()` once; every live
update uses `series.update(candle)` for the single changed bar. Indicators are
incremental — a tick updates the last EMA/BB point, not the whole series. The
chart instance lives in a ref, never in React state, so ticks do not re-render
the tree.

**Timeframe state:** the chart defaults to `settings.trading.timeframe` (the
existing dashboard timeframe) and only diverges when the user picks a chart
timeframe explicitly — so "Dashboard 15m / Chart 5m" cannot happen by accident.

**Signal markers** read the existing `signals` array already in the zustand
store (`store.ts` pushes `signal` events). The chart generates nothing.

### Explicitly not touched

Strategy generation · confidence · risk management · stake sizing · execution ·
market regime · adaptive learning · decision engine · broker authentication ·
`volume_source` protection.

---

## 5. Verification plan

Frontend: `npm run build` (runs `tsc -b` first, so typecheck + build together),
`npm run lint`. Backend: the full pytest suite with
`PYTHONPATH=backend:vendor/old-pyquotex`, currently **620 passed / 1 skipped**.

New tests cover the brief's 12 cases, including the required one: start on 15m,
feed ticks inside the same bucket → one candle keeps updating; cross the
boundary → previous finalizes, new one starts, no duplicate timestamp, no gap,
no fabricated candle.

---

## 6. Open questions before I write code

1. **`GET /api/chart/candles` alongside `GET /api/candles`, or migrate the
   chart onto the existing endpoint?** I propose adding the new one and leaving
   the old one alone, since other callers may depend on it.
2. **`volume: null` for synthetic volume** — agreed? The alternative (sending
   the tick count) would display a number the model itself says is not real
   volume.
3. **`1D` label vs the model's `"1day"`** — I will map the label and keep the
   wire value `"1day"`, so no existing timeframe handling changes.
4. **Throttle interval for `candle_update`** — I propose ~1s per watched
   asset/timeframe. Fast enough to feel live, cheap enough not to spam.
