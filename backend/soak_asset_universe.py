"""Asset-universe soak: verify the scanner tracks the universe 0 -> N -> 0 -> N
across market open/close and broker outages, with no manual restart.

Run:  cd backend && python3 soak_asset_universe.py [seconds]
"""
from __future__ import annotations

import asyncio
import sys
import time
import types as _t
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.schemas import AssetInfo  # noqa: E402
from app.services.market import (  # noqa: E402
    ASSET_SNAPSHOT_STALE_TTL,
    AssetFeedSnapshot,
    PyQuotexProvider,
)


def row(symbol, is_open=True, payout=85.0):
    r = [0] * 20
    r[1] = symbol
    r[2] = symbol + "\n"
    r[5] = payout
    r[14] = is_open
    r[len(r) - 9] = payout
    return r


class Broker:
    """Simulates the broker's instrument feed: market hours, outages."""

    def __init__(self, n=57):
        self.n = n
        self.market_open = True
        self.fail = None
        self.calls = 0

    async def get_instruments(self):
        self.calls += 1
        if self.fail:
            raise self.fail
        return [row(f"A{i}", is_open=self.market_open) for i in range(self.n)]


class Ready:
    def __init__(self):
        self.ready = set()

    def is_ready(self, s):
        return s in self.ready


def build(broker):
    from app.orchestrator import Orchestrator
    p = PyQuotexProvider.__new__(PyQuotexProvider)
    p._client = broker
    p._connected = True
    p._asset_feed = AssetFeedSnapshot()
    p._last_good_assets = []
    # Mirrors __init__ (bypassed here via __new__): refreshes are
    # serialized so a slow empty response cannot overwrite a newer valid
    # snapshot.
    p._asset_refresh_lock = asyncio.Lock()

    o = Orchestrator.__new__(Orchestrator)
    o.provider = p
    o._assets_cache = []
    o._assets_ts = 0.0
    o._asset_funnel = {}
    o.runtime = _t.SimpleNamespace(
        trading=_t.SimpleNamespace(asset_whitelist=[]),
        risk=_t.SimpleNamespace(min_payout=75.0),
    )
    o.market_init = Ready()
    return o, p


async def main(duration=60.0):
    broker = Broker(57)
    o, p = build(broker)
    o.market_init.ready = {f"A{i}" for i in range(57)}

    print("=" * 74)
    print(f"ASSET-UNIVERSE SOAK — {duration:.0f}s, market open/close + broker outage")
    print("=" * 74)

    phases = [
        ("open",      0.16, True,  None, "market open"),
        ("closed",    0.16, False, None, "market closed"),
        ("reopen",    0.16, True,  None, "market reopens — no restart"),
        ("outage",    0.16, True,  ConnectionError("feed down"), "broker outage"),
        ("recovered", 0.18, True,  None, "outage clears"),
        ("closed2",   0.18, False, None, "market closes again"),
    ]

    observed = []
    failures = []
    t_start = time.monotonic()

    for name, frac, is_open, fail, label in phases:
        broker.market_open = is_open
        broker.fail = fail
        # Force the stale window to lapse so an outage phase reaches ERROR.
        if fail:
            p._asset_feed.fetched_at = time.time() - (ASSET_SNAPSHOT_STALE_TTL + 1)
        t0 = time.monotonic()
        counts, statuses = set(), set()
        while time.monotonic() - t0 < duration * frac:
            o._assets_ts = 0.0                      # emulate the 10s refresh
            cands = await o._candidate_assets()
            f = o.asset_funnel()
            counts.add(len(cands))
            statuses.add(f.get("status"))
            await asyncio.sleep(0.05)

        final = len(cands)
        st = o.asset_funnel()
        observed.append((name, final, st.get("status")))

        if name in ("open", "reopen", "recovered"):
            # 12, not 57: _candidate_assets() intentionally caps at the
            # top-12 by payout. The full universe is checked via the
            # funnel's eligible_before_ready_gate.
            ok = (final == 12 and st["status"] == "ok"
                  and st.get("eligible_before_ready_gate") == 57
                  and st.get("open_count") == 57)
        elif name in ("closed", "closed2"):
            ok = final == 0 and st["status"] == "ok" and st["open_count"] == 0
        else:  # outage
            ok = final == 0 and st["status"] == "error" and st["last_instrument_error"]
        if not ok:
            failures.append(f"{name}: candidates={final} status={st.get('status')}")
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:10s} {label:32s} "
              f"candidates={final:3d}/12  eligible={st.get('eligible_before_ready_gate')}  "
              f"status={str(st.get('status')):6s}  open={st.get('open_count')}")

    elapsed = time.monotonic() - t_start
    seq = [c for _n, c, _s in observed]
    print("\n" + "-" * 74)
    print(f"  candidate sequence : {seq}")
    print(f"  broker calls       : {broker.calls}")
    print(f"  elapsed            : {elapsed:.0f}s")
    print("-" * 74 + "\n")

    checks = [
        ("scanner went N -> 0 when the market closed", seq[0] == 12 and seq[1] == 0),
        ("scanner returned 0 -> N on reopen, no restart", seq[2] == 12),
        ("broker outage reported as ERROR, not '0 open assets'",
         observed[3][2] == "error"),
        ("scanner recovered automatically after the outage", seq[4] == 12),
        ("scanner tracked a second close", seq[5] == 0),
        ("every phase matched its expectation", not failures),
    ]
    bad = 0
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1
    for f in failures:
        print(f"    detail: {f}")

    print("\n" + "=" * 74)
    print(f"SOAK RESULT: {len(checks) - bad}/{len(checks)} checks passed over {elapsed:.0f}s")
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    raise SystemExit(asyncio.run(main(dur)))
