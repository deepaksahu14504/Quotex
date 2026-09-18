"""
Test script for PyQuotexProvider - Har API call check karega
Run: python -m app.core.test_market
"""
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.market import PyQuotexProvider, SimulatedProvider
from app.schemas import Direction

# Detailed logging set karo
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class QuotexTester:
    """Step-by-step tester for PyQuotex provider"""
    
    def __init__(self, email: str, password: str, is_demo: bool = True):
        self.provider = PyQuotexProvider(
            email=email,
            password=password,
            is_demo=is_demo
        )
        self.results = {
            'connect': False,
            'balance': False,
            'assets': False,
            'candles': False,
            'payout': False,
            'order': False,
            'check_result': False
        }
    
    async def test_connect(self):
        """Test 1: Connection aur authentication"""
        print("\n" + "="*60)
        print("TEST 1: CONNECTION CHECK")
        print("="*60)
        
        connected = await self.provider.connect()
        assert connected, "Connection FAILED"
        assert self.provider.connected, "connected property FALSE hai"
        
        self.results['connect'] = True
        print("✅ Connection SUCCESS")
        
        # Double connect test
        connected2 = await self.provider.connect()
        print(f"✅ Double connect handled: {connected2}")
    
    async def test_balance(self):
        """Test 2: Balance check"""
        print("\n" + "="*60)
        print("TEST 2: BALANCE CHECK")
        print("="*60)
        
        balance, currency = await self.provider.get_balance()
        print(f"💰 Balance: {balance} {currency}")
        
        assert balance > 0, f"Balance 0 ya negative: {balance}"
        self.results['balance'] = True
        print("✅ Balance SUCCESS")
    
    async def test_assets(self):
        """Test 3: Assets loading"""
        print("\n" + "="*60)
        print("TEST 3: ASSETS CHECK")
        print("="*60)
        
        assets = await self.provider.get_assets()
        print(f"📊 Total assets loaded: {len(assets)}")
        
        assert len(assets) > 0, "ZERO assets mile"
        
        # Show first 5
        for asset in assets[:5]:
            print(f"  • {asset.symbol:20s} | {asset.name:30s} | {'OPEN' if asset.is_open else 'CLOSED'} | Payout: {asset.payout}%")
        
        # Check sab open hain
        closed = [a for a in assets if not a.is_open]
        if closed:
            print(f"⚠️  {len(closed)} CLOSED assets bhi return hue")
        
        # Cache test
        assets2 = await self.provider.get_assets()
        assert len(assets2) == len(assets), "Cache se same count nahi aaya"
        print("✅ Cache working")
        
        self.results['assets'] = True
        print(f"✅ Assets SUCCESS ({len(assets)} assets)")
    
    async def test_payout(self):
        """Test 4: Payout for specific asset"""
        print("\n" + "="*60)
        print("TEST 4: PAYOUT CHECK")
        print("="*60)
        
        # Pehle assets lo
        assets = await self.provider.get_assets()
        if not assets:
            print("❌ No assets for payout test")
            return
        
        # Test first 3 assets
        for asset in assets[:3]:
            payout = await self.provider.get_payout(asset.symbol, "1m")
            print(f"  Payout {asset.symbol}: {payout}%")
            assert payout > 0, f"Payout 0 for {asset.symbol}"
        
        self.results['payout'] = True
        print("✅ Payout SUCCESS")
    
    async def test_candles(self):
        """Test 5: Candle data"""
        print("\n" + "="*60)
        print("TEST 5: CANDLES CHECK")
        print("="*60)
        
        assets = await self.provider.get_assets()
        if not assets:
            print("❌ No assets for candle test")
            return
        
        # Test multiple timeframes
        test_asset = assets[0].symbol
        timeframes = ["1m", "5m", "15m"]
        
        for tf in timeframes:
            candles = await self.provider.get_candles(test_asset, tf, count=10)
            print(f"\n  📈 {test_asset} {tf}: {len(candles)} candles")
            
            if candles:
                # Check sorting
                timestamps = [c.timestamp for c in candles]
                assert timestamps == sorted(timestamps), f"❌ Timestamps sorted nahi hain {tf}"
                
                # Show first aur last candle
                first = candles[0]
                last = candles[-1]
                print(f"    First: {first.timestamp} | O:{first.open:.5f} H:{first.high:.5f} L:{first.low:.5f} C:{first.close:.5f}")
                print(f"    Last:  {last.timestamp} | O:{last.open:.5f} H:{last.high:.5f} L:{last.low:.5f} C:{last.close:.5f}")
                
                # Validate candle structure
                for c in candles:
                    assert c.high >= c.low, f"High < Low in candle {c.timestamp}"
                    assert c.high >= c.open and c.high >= c.close, "High smaller than O/C"
                    assert c.low <= c.open and c.low <= c.close, "Low greater than O/C"
            else:
                print(f"    ⚠️  EMPTY candles for {tf}")
        
        self.results['candles'] = True
        print(f"\n✅ Candles SUCCESS")
    
    async def test_order_flow(self):
        """Test 6: Full order lifecycle"""
        print("\n" + "="*60)
        print("TEST 6: ORDER FLOW (PAPER TRADE)")
        print("="*60)
        
        assets = await self.provider.get_assets()
        if not assets:
            print("❌ No assets for order test")
            return
        
        # Test with minimum amount
        test_asset = assets[0].symbol
        amount = 1.0  # $1 trade
        duration = 60  # 1 minute
        
        print(f"🎯 Placing order: {test_asset} CALL ${amount} {duration}s")
        
        try:
            order_id, open_price = await self.provider.place_order(
                asset=test_asset,
                amount=amount,
                direction=Direction.CALL,
                duration=duration
            )
            print(f"✅ Order placed: ID={order_id}, Open={open_price}")
            
            # Check result immediately
            result = await self.provider.check_result(order_id)
            if result:
                print(f"📊 Immediate result: {result['status']} | P/L: ${result['profit']}")
            else:
                print("⏳ Trade pending (normal for 60s duration)")
                
                # Wait and check
                print("Waiting for trade to complete...")
                for i in range(70):
                    await asyncio.sleep(1)
                    result = await self.provider.check_result(order_id)
                    if result:
                        print(f"📊 Final result after {i+1}s: {result['status']} | P/L: ${result['profit']}")
                        break
                else:
                    print("⚠️  Trade didn't resolve in 70s")
            
            self.results['order'] = True
            print("✅ Order Flow SUCCESS")
            
        except Exception as e:
            print(f"❌ Order failed: {e}")
            # Demo accounts ke liye normal hai kabhi kabhi
    
    async def test_disconnect(self):
        """Test 7: Clean disconnect"""
        print("\n" + "="*60)
        print("TEST 7: DISCONNECT")
        print("="*60)
        
        await self.provider.disconnect()
        assert not self.provider.connected, "connected still TRUE after disconnect"
        print("✅ Disconnect SUCCESS")
    
    def print_summary(self):
        """Final summary"""
        print("\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        
        for test, passed in self.results.items():
            status = "✅" if passed else "❌"
            print(f"  {status} {test}")
        
        total = len(self.results)
        passed = sum(self.results.values())
        print(f"\n🎯 {passed}/{total} tests PASSED")
        
        if passed == total:
            print("\n🔥 BADHAI HO! Provider perfect hai!")
        else:
            print(f"\n⚠️  {total - passed} tests FAILED - logs check karo")


async def test_simulated():
    """Quick test for SimulatedProvider"""
    print("\n" + "="*60)
    print("SIMULATED PROVIDER QUICK TEST")
    print("="*60)
    
    provider = SimulatedProvider()
    await provider.connect()
    
    # Balance
    bal, curr = await provider.get_balance()
    print(f"💰 Balance: {bal} {curr}")
    
    # Assets
    assets = await provider.get_assets()
    print(f"📊 Assets: {len(assets)}")
    
    # Candles
    candles = await provider.get_candles("EURUSD_otc", "1m", 5)
    print(f"📈 Candles: {len(candles)}")
    
    # Order
    oid, price = await provider.place_order("EURUSD_otc", 10, Direction.CALL, 5)
    print(f"🎯 Order: {oid}")
    
    await asyncio.sleep(6)
    result = await provider.check_result(oid)
    print(f"📊 Result: {result['status']} P/L: ${result['profit']}")
    
    await provider.disconnect()
    print("✅ SimulatedProvider working perfectly")


async def main():
    """Main test runner"""
    
    # Configuration - YAHAN APNI DETAILS DALO
    EMAIL = "your_email@example.com"  # ← APNA EMAIL DALO
    PASSWORD = "your_password"         # ← APNA PASSWORD DALO
    IS_DEMO = True
    
    print("🚀 PyQuotex Provider Test Suite")
    print(f"   Demo: {IS_DEMO}")
    print(f"   Email: {EMAIL}")
    
    # Simulated first (no credentials needed)
    print("\n1️⃣ Testing Simulated Provider...")
    await test_simulated()
    
    # Check if credentials provided
    if EMAIL == "your_email@example.com":
        print("\n⚠️  Real provider ke liye email/password set karo")
        print("   Edit karo: EMAIL aur PASSWORD variables")
        return
    
    # Real provider test
    print("\n2️⃣ Testing PyQuotex Provider...")
    tester = QuotexTester(EMAIL, PASSWORD, IS_DEMO)
    
    try:
        await tester.test_connect()
        await tester.test_balance()
        await tester.test_assets()
        await tester.test_payout()
        await tester.test_candles()
        await tester.test_order_flow()
    except Exception as e:
        logger.exception("Test failed with error")
    finally:
        await tester.test_disconnect()
        tester.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
