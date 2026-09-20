# P0 / P1 — Market-data & feature RCA (inspect-only)

Repo: `/home/user/Quotex` · branch `arena/01a0bee8-quotex` · HEAD `64e2c43`
Every line below was produced by reading or running code in this workspace. Nothing
here is inferred from upstream pyquotex documentation.

---

## A. What the installed vendor actually exposes

Source of truth: `vendor/old-pyquotex/pyquotex/` (65 `.py` files — the only committed
vendor copy; `vendor/pyquotex/` is empty and untracked).

### A.1 OHLC history
`PyQuotexProvider.get_candles()` calls `self._client.get_candles(asset, time.time(), period*6, period)`
(`backend/app/services/market.py:1692`). Returns `list[dict]` with `time` + OHLC.
**There is no volume key in the pyquotex history payload** — confirmed at
`api.py:790` (the tick frame carries only `asset, ts, price, direction`).

### A.2 Realtime price / ticks
- `_api/realtime.py:417` `start_realtime_price(asset, period, timeout)`
- `_api/realtime.py:500` `get_realtime_price(asset) -> list[dict]` — returns
  `list(self.api.realtime_price.get(asset, []))`
- Producer: `api.py:783-796`. A 4-element tick frame `[asset, ts, price, direction]`
  is appended as `{"time": ts, "price": price}` to a **1000-entry left-evicting list**,
  and `realtime_candles[asset] = message[0]`.
- **Only 3 of the 4 elements are stored.** `direction` is dropped by the handler even
  though `utils/processor.py:23` (`symbol, timestamp, price, direction = tick`) proves
  the wire format carries it.
- `_api/realtime.py:459` `start_realtime_candle()` + `utils/processor.py:17` `process_tick()`
  fold ticks into OHLC themselves.

**Consequence:** tick *count* is the only activity signal available. There is no
traded-volume field anywhere on this path. Tick count must never be presented as
financial volume.

### A.3 Realtime trader sentiment — **genuinely available**
- `_api/realtime.py:438` `start_realtime_sentiment(asset, period, timeout)` →
  returns `self.api.realtime_sentiment[asset]`
- `_api/realtime.py:494` `get_realtime_sentiment(asset) -> dict` →
  `self.api.realtime_sentiment.get(asset, {})`
- Storage: `api.py:136-137` `realtime_sentiment: dict = {}`, `traders_mood: dict = {}`
- Handler: `api.py:363-369` `_h_sentiment(data)` stores the **raw dict as-is** under
  `data["asset"]` in both dicts.
- Registered in the control dispatcher at `api.py:171` (`"sentiment": self._h_sentiment`).
- **Payload keys** — from the vendor's own consumer, `cli/commands/realtime.py:59-60`:
  `sentiment.get("call", sentiment.get("bulls", "?"))` and
  `sentiment.get("put", sentiment.get("bears", "?"))`, rendered as `CALL {n}% / PUT {n}%`.
  So the percentages arrive under `call`/`put` with `bulls`/`bears` as aliases.
- `_api/realtime.py:587` `start_mood_stream()` guards on
  `hasattr(api, "subscribe_Traders_mood")` / `hasattr(api, "traders_mood")`, so the
  *mood* path is conditional. The **sentiment** path is not.

### A.4 Not available
Real order-book volume, order flow, bid/ask depth, spread. Any "volume" number for
Quotex is a tick-count proxy and must stay labelled as such.

---

## B. Data flow **before** this change

```
pyquotex ws frame [asset, ts, price, direction]
        │  api.py:783-796  (direction DROPPED)
        ▼
api.realtime_price[asset]  ── 1000-entry list, left-evicting
        │  market.py:1728  get_realtime_price(asset)
        ▼
PyQuotexProvider.get_candles()  market.py:1735-1767
        │  bucket = ts // period * period
        │  row["volume"] += 1.0        ◄── TICK COUNT WRITTEN INTO `volume`
        │  row["ticks"]  += 1
        │  row["_volume_source"] = "synthetic"
        ▼
Candle(volume=tick_count, volume_source="synthetic")   schemas.py:46
        │
        ├─► CandleStore.upsert_many()  ── volume_source NOT PERSISTED
        │        │  candle_store.py:104-108 (INSERT has no volume_source column)
        │        ▼  candle_store.py:48-54 / 71-72 (read back volume_source="real")
        │   Candle(volume=tick_count, volume_source="real")   ◄── TAG LOST
        │
        └─► IndicatorCache.get_enriched()  incremental.py:412
                 │  _carry_volume_source()  incremental.py:38  (F5 fix, commit f1f2706)
                 ▼
            _has_real_volume(df)  indicators.py:384
                 ▼
            volume_ma / volume_strength / price_action_strength
```

### B.1 Sentiment is completely unused
`grep -rniE "sentiment|traders_?mood" --include=*.py app/` → **zero matches.**
The broker publishes trader sentiment and the application never reads it.

### B.2 Existing feature columns (do not duplicate)
Produced by `incremental.py` / `indicators.py` / `strategies.py`:

```
adx atr bb_width confluence ema5 ema8 ema9 ema13 ema21 ema50 macd_hist
price_strength psar_dir resistance resistance_strength rsi srsi_d st_dir
stoch_d support support_strength time_vol_mult vol_ma vol_ratio vol_strength
```

`support`/`resistance` are **levels**, not distances. `atr` exists but no
ATR-normalised movement. `price_strength` blends body+wick into one 0-1 score but
exposes no body/wick ratios individually. **None of the requested price-action or
tick features already exist as columns.**

### B.3 Tick state keying is already correct
`market.py:1026 _new_ticks()` and `:1061 _advance_tick_cursor()` key their cursor on
`(asset, period)` — `self._stream_tick_ts: Dict[Tuple[str, int], Tuple[float, int]]`
(`:658`). The docstring explicitly warns that a cursor shared across timeframes
starves whichever timeframe reads second, and that the broker buffer is bounded at
1000 so position-based cursors break. `_streamed` is also `(asset, period)` (`:610`).
This keying must be preserved, not reimplemented.

---

## C. Findings

### C.1 **[HIGH] The DB round-trip launders synthetic tick count into "real" volume**

`CandleStore.upsert_many()` inserts only `(ts, open, high, low, close, volume)`
(`candle_store.py:104-108`) — `volume_source` is on the Pydantic model but has no
column. Both readers then hardcode `volume_source="real"` (`:54`, `:72`), with a
comment calling it a "safe default for public-provider" data.

For the pyquotex path that default is wrong, and it re-enables exactly the bug
`_has_real_volume()`'s own docstring was written to prevent:

```
BEFORE db : source=synthetic _has_real_volume=False
AFTER  db : source=real      _has_real_volume=True
volume_strength (synthetic SHOULD be neutral 1.0): 0.5000
```

Measured this session with `pgserver` on 40 synthetic candles
(`volume = 1 + i%3`, `volume_source="synthetic"`). After the round-trip
`_has_real_volume` flips to `True`, so `volume_strength` returns **0.5 instead of
the neutral 1.0** that `indicators.py:441-442` promises. That value feeds
`strategies.py:133` `df["vol_strength"]`.

**Reachable in production:** `orchestrator.py:2850` upserts every scan,
`orchestrator.py:3486` and `validation_service.py:233,239` read back via
`get_latest_n` / `fetch_or_load`. `fetch_or_load` prefers the cache and only hits the
network when it is short, so cached rows — the ones carrying the laundered tag — are
what the engine normally consumes.

### C.2 **[HIGH] Tick count is written into the `volume` field itself**
`market.py:1762` `row["volume"] = float(row.get("volume", 0) or 0) + 1.0`, seeded at
`:1749` `"volume": 1.0`. The tag correctly says `synthetic`, and `_has_real_volume`
correctly neutralises it **in memory** — but the number still travels in a field named
`volume`, so every consumer that forgets the tag (C.1) silently does volume maths on
tick counts. The activity count already lives separately in `row["ticks"]`.

### C.3 **[MEDIUM] Sentiment is available but never consumed** (B.1)

### C.4 **[MEDIUM] No price-action / microstructure feature layer**
`price_action_strength()` (`indicators.py:447-484`) computes body ratio and wick
rejection **inline and discards them**, returning a single blended 0-1 score. Nothing
exposes body ratio, wick ratios, CLV, range vs ATR, momentum, consecutive direction,
or breakout/S-R distance to strategies or observability.

### C.5 **[LOW] Tick `direction` is dropped at the handler**
`api.py:785` unpacks 3 of 4 elements. `tick_direction_balance` therefore cannot be
computed from `get_realtime_price()`; it has to be derived from signed intrabar price
changes instead. This is a vendor limitation, not something to patch in the vendor.

### C.6 Sound — verified, do not weaken
- `_has_real_volume()` honours the tag first and only falls back to the
  variance+sum heuristic for untagged frames (`indicators.py:404-424`).
- `volume_ma` / `volume_strength` return neutral `1.0` on synthetic volume
  (`:432-434`, `:441-443`) — strategies are never blocked by a fake volume gate.
- `price_action_strength()` requires no volume; volume is an optional 20% blend
  (`:474-483`).
- Tick dedup uses strict `>` on timestamp plus an intra-boundary position counter, so
  the additive volume proxy cannot be double-counted (`market.py:1026-1060`).
- `_carry_volume_source()` propagates the tag on all three warm-path row builds
  (`incremental.py:38`, called at `:464`).

---

## D. Scope decisions taken from the above

1. Keep `volume_source` and the `_has_real_volume` guard. Extend them; do not delete.
2. Fix C.1 by persisting the tag, not by weakening the guard.
3. Keep `volume=0` where real volume is absent. Activity moves to separate
   tick-feature fields, never into `volume`.
4. Build the feature layer as **new columns** alongside the existing ones; no existing
   indicator mathematics is rewritten.
5. Sentiment enters as a bounded, optional, TTL-gated confidence adjustment on top of
   the existing `regime_adj` path (`orchestrator.py:2203`, `:3162`). It never sets
   direction and never reverses a signal.
6. Reuse the existing `(asset, period)` keying for all new realtime state.
