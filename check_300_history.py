import sys
import json
import urllib.request
import urllib.parse
import urllib.error

BASE = "http://127.0.0.1:8090"

asset = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
timeframe = sys.argv[2] if len(sys.argv) > 2 else "1m"
count = 300

print("=" * 70)
print("QUOTEX 300-CANDLE HISTORY TEST")
print("=" * 70)
print(f"Asset     : {asset}")
print(f"Timeframe : {timeframe}")
print(f"Requested : {count}")
print()

# ---------------------------------------------------------
# 1. Direct candle request
# ---------------------------------------------------------
params = urllib.parse.urlencode({
    "asset": asset,
    "timeframe": timeframe,
    "count": count,
})

url = f"{BASE}/api/candles?{params}"

print("[1] Requesting 300 candles...")
print(f"    {url}")
print()

try:
    with urllib.request.urlopen(url, timeout=60) as r:
        raw = r.read().decode()
        data = json.loads(raw)

    if isinstance(data, list):
        candles = data
        print("SUCCESS: API returned candle list")
        print(f"Received candles : {len(candles)}")
        print(f"Required         : {count}")
        print()

        if len(candles) >= count:
            print("RESULT: PASS - 300 candles available")
        else:
            print("RESULT: FAIL - insufficient candles")
            print(f"Missing          : {count - len(candles)}")

        if candles:
            print()
            print("First candle:")
            print(json.dumps(candles[0], indent=2))

            print()
            print("Last candle:")
            print(json.dumps(candles[-1], indent=2))

    else:
        print("ERROR: API returned unexpected data:")
        print(json.dumps(data, indent=2))

except urllib.error.HTTPError as e:
    print(f"HTTP ERROR: {e.code} {e.reason}")
    try:
        print(e.read().decode())
    except Exception:
        pass

except Exception as e:
    print(f"REQUEST ERROR: {type(e).__name__}: {e}")

# ---------------------------------------------------------
# 2. Market-init diagnostic
# ---------------------------------------------------------
print()
print("=" * 70)
print("[2] MARKET INIT DIAGNOSTIC")
print("=" * 70)

try:
    with urllib.request.urlopen(
        f"{BASE}/api/market-init",
        timeout=15
    ) as r:
        init_data = json.loads(r.read().decode())

    assets = init_data.get("assets", {})

    rec = assets.get(asset)

    if rec:
        print(json.dumps(rec, indent=2))
    else:
        print(f"No market-init record found for {asset}")
        print()
        print("Available assets containing this symbol:")
        for symbol in assets:
            if asset.upper() in symbol.upper():
                print(" ", symbol)

except Exception as e:
    print(f"MARKET-INIT ERROR: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("END")
print("=" * 70)
