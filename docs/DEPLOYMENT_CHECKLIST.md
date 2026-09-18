# Deployment Checklist - Signal Architecture Polish

Use this checklist to verify all improvements are properly integrated and working.

## Pre-Deployment Verification

### Code Quality
- [ ] All new files have proper docstrings
- [ ] Type hints present on all functions
- [ ] No console.log statements (only logger.*)
- [ ] Import statements organized and clean
- [ ] No unused imports

### Testing
- [ ] Test error_recovery.py CircuitBreaker state transitions
- [ ] Test queue_persistence.py load/save cycles
- [ ] Test strategy_correlation.py with various vote distributions
- [ ] Test production_metrics.py with sample data
- [ ] Verify orchestrator.py TTL calculations for each timeframe

### Integration
- [ ] Imports added to orchestrator.py
- [ ] TTL calculation tested for 1m, 5m, 15m timeframes
- [ ] Dead-market filter thresholds verified
- [ ] Multi-trade logic tested (allow 2 per asset)
- [ ] HTF fallback logic functional

---

## Deployment Steps

### Step 1: Pre-Deployment Checks
```bash
# Navigate to project
cd /vercel/share/v0-project

# Check syntax
python -m py_compile QuotexAutoTrader/backend/app/engine/error_recovery.py
python -m py_compile QuotexAutoTrader/backend/app/engine/queue_persistence.py
python -m py_compile QuotexAutoTrader/backend/app/engine/strategy_correlation.py
python -m py_compile QuotexAutoTrader/backend/app/engine/production_metrics.py

# Verify imports
python -c "from QuotexAutoTrader.backend.app.engine.error_recovery import CircuitBreaker"
python -c "from QuotexAutoTrader.backend.app.engine.queue_persistence import QueuePersistence"
python -c "from QuotexAutoTrader.backend.app.engine.strategy_correlation import StrategyCorrelationGuard"
python -c "from QuotexAutoTrader.backend.app.engine.production_metrics import ProductionMetrics"
```

### Step 2: Database/Storage
- [ ] Ensure `./data/queue/` directory can be created
- [ ] Verify write permissions in data directory
- [ ] Check for sufficient disk space

### Step 3: Initialization
In `orchestrator.py` `__init__` method, add:

```python
# Import at top
from .engine.error_recovery import get_recovery
from .engine.queue_persistence import get_persistence
from .engine.strategy_correlation import StrategyCorrelationGuard
from .engine.production_metrics import get_metrics

# In __init__ after existing code
self.error_recovery = get_recovery()
self.queue_persistence = get_persistence()
self.correlation_guard = StrategyCorrelationGuard()
self.metrics = get_metrics()
```

### Step 4: Verify Monitoring
- [ ] Check logs contain TTL calculations
- [ ] Verify metrics reporting starts on boot
- [ ] Test health indicator computation
- [ ] Confirm alerts trigger on threshold violations

### Step 5: Production Rollout
- [ ] Deploy to staging first
- [ ] Monitor for 24 hours
- [ ] Check signal execution rates
- [ ] Verify no crash loops
- [ ] Confirm queue persistence working

---

## Post-Deployment Monitoring

### Hour 1
- [ ] System boots without errors
- [ ] No import errors in logs
- [ ] Initial metrics reporting working
- [ ] First signals generating successfully

### Day 1
- [ ] Execution success rate > 92%
- [ ] Queue wait times < 300ms
- [ ] No crash/restart loops
- [ ] Dead-market filter rejecting properly

### Week 1
- [ ] Execution success rate stabilized > 94%
- [ ] False positive rate < 8%
- [ ] Per-strategy metrics make sense
- [ ] Health indicators mostly green

### Week 2-4
- [ ] Profitability trending up 5-10%
- [ ] No anomalies in correlation analysis
- [ ] Queue persistence tested (restart system)
- [ ] All tiers functioning nominally

---

## Performance Baseline

Record these metrics before and after deployment:

### Before Deployment
```
Date: ___________
Execution Rate: _______%
Avg Queue Wait: _______ms
Win Rate: _______%
False Positives: _______%
System Uptime: _______%
```

### After Deployment (24h)
```
Date: ___________
Execution Rate: _______%
Avg Queue Wait: _______ms
Win Rate: _______%
False Positives: _______%
System Uptime: _______%
```

### Improvement Tracking
```
Execution Rate Change: +_____%
Queue Wait Improvement: _____%
Win Rate Change: +_____%
False Positive Reduction: _____%
Uptime Improvement: +_____%
```

---

## Troubleshooting Guide

### Issue: Circuit Breaker Always Open
**Symptoms:** All API calls failing immediately  
**Check:**
- Verify broker API is responding
- Check network connectivity
- Review error_recovery.py timeout settings
- Increase failure threshold if transient issues

### Issue: Queue Not Persisting
**Symptoms:** Signals lost on restart  
**Check:**
- Verify `./data/queue/` directory writable
- Check disk space available
- Review queue_persistence.py file I/O
- Ensure no file permission issues

### Issue: Dead-Market Filter Too Strict
**Symptoms:** Too many signals rejected  
**Check:**
- Verify ADX threshold (14 is correct)
- Check BB width threshold (0.55)
- Review RSI extreme settings (>85 or <15)
- Consider adjusting thresholds if false negatives too high

### Issue: Multiple Trades Not Executing
**Symptoms:** Second trade on asset blocked  
**Check:**
- Verify orchestrator.py multi-trade logic
- Check asset collision detection
- Review direction checking (should allow different directions)
- Confirm max_per_asset = 2

### Issue: Metrics Not Updating
**Symptoms:** Stale health indicators  
**Check:**
- Verify record_signal() called for each signal
- Verify record_execution() called after placement
- Check metrics.print_report() in logs
- Review production_metrics.py initialization

---

## Rollback Plan

If issues arise, rollback using git:

```bash
# Check current state
git log --oneline -5

# Rollback if needed
git revert <commit_hash>

# Or reset to pre-polish state
git reset --hard HEAD~1

# Verify rollback
python -c "import orchestrator; print('OK')"
```

---

## Success Criteria

### All of the following must be true:
- [ ] System starts without errors
- [ ] No import failures in logs
- [ ] Execution success rate > 92%
- [ ] Average queue wait < 300ms
- [ ] Health indicators reporting
- [ ] Alerts generating on violations
- [ ] Per-strategy metrics visible
- [ ] No crash loops in 24 hours
- [ ] Win rate stable
- [ ] False positives declining

---

## Communication

### If Deploying to Team:
- [ ] Document which files changed
- [ ] Explain each tier's improvements
- [ ] Show expected vs actual results
- [ ] Provide troubleshooting guide
- [ ] Schedule daily standup for week 1

### Logging & Alerts
- [ ] Configure log aggregation (CloudWatch, etc.)
- [ ] Set up alerts for health degradation
- [ ] Configure dashboard for metrics
- [ ] Enable real-time notifications

---

## Sign-Off

- [ ] All checks passed
- [ ] Ready for production deployment
- [ ] Team informed and trained
- [ ] Monitoring configured
- [ ] Rollback plan ready

**Deployed By:** ________________  
**Date:** ________________  
**Version:** architecture-polish-v1.0
