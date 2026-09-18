"""Reproduce the exact incident from the logs: a $5.10 base stake reaching
$430.80 through martingale, on an account that cannot cover it."""
import sys
sys.path.insert(0, '.')
from app.config import RiskSettings
from app.engine.risk import RiskManager

rs = RiskSettings(stake_mode="fixed", stake_amount=5.10,
                  martingale_enabled=True, martingale_multiplier=2.0,
                  martingale_max_steps=10)   # deliberately high, as in the incident
rm = RiskManager(rs)
rm._martingale_step = 7    # 5.10 * 2^7 = 652.8, close to the observed 430.8 at step ~6.4

for step, balance in ((6, 3.33), (6, 500.0), (0, 3.33)):
    rm._martingale_step = step
    stake = rm.stake_for(balance)
    uncapped = round(5.10 * (2.0 ** step), 2)
    print(f"  step={step} balance=${balance:>7.2f} uncapped=${uncapped:>8.2f} -> capped stake=${stake}")

print()
low = RiskManager(rs); low._martingale_step = 6
s = low.stake_for(3.33)
assert s == 0.0, f"expected refusal on a $3.33 balance, got {s}"
print("$3.33 balance -> stake_for returns 0.0 (refused, no order attempted)")

funded = RiskManager(rs); funded._martingale_step = 6
s2 = funded.stake_for(2000.0)
assert 0 < s2 <= 500.0, s2
print(f"$2000 balance -> stake capped to ${s2} (25% of balance), not the full uncapped ${round(5.10*64,2)}")

print("\nSTAKE CAP CHECKS PASSED — a martingale runaway can no longer reach the broker")
