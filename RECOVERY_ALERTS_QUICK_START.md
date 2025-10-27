# Recovery Alerts Testing - Quick Reference

## 🚀 Fastest Way to Test

Run the interactive helper:
```bash
python test_automation_helper.py
```

Then choose option **1** to send a live test email.

---

## 📧 Send Live Email (Quick Test)

```bash
python tests/test_recovery_alerts_live.py
```

This will:
- Create test athlete with breached metrics
- Send REAL email to your HEAD_COACH_EMAIL
- Clean up all test data automatically

**Check your email inbox!**

---

## 🔧 What Was Changed/Created

### New Files Created:

1. **`tests/test_recovery_alerts_live.py`**
   - Live email integration tests
   - Sends actual emails via SendGrid
   - Tests with real and test athlete data

2. **`tests/test_automation.py`**
   - Automation testing utilities
   - Time simulation scenarios
   - Mock time progression tests
   - Helper class for creating test data

3. **`test_automation_helper.py`**
   - Interactive menu-driven interface
   - Easy access to all test functions
   - Create custom test scenarios
   - Control scheduler manually

4. **`RECOVERY_ALERTS_TESTING_GUIDE.md`**
   - Complete testing documentation
   - Troubleshooting guide
   - Best practices

### Modified Files:

1. **`app/scheduling/scheduler.py`**
   - **ENHANCED**: Now includes recovery alert evaluation
   - Daily job now does 2 things:
     1. Ingest recent data from TrainingPeaks
     2. Evaluate recovery alerts for ALL athletes
   - Better logging and error handling

---

## ⚡ Quick Test Commands

### Test 1: Live Email
```bash
python tests/test_recovery_alerts_live.py
```

### Test 2: Multi-Day Simulation
```python
from tests.test_automation import test_time_simulation_scenario
test_time_simulation_scenario()
```

### Test 3: Run Daily Job Manually
```python
from app.scheduling.scheduler import daily_job
result = daily_job()
```

### Test 4: Interactive Menu
```bash
python test_automation_helper.py
```

---

## 🎯 Test Scenarios Included

### Live Email Tests
- ✅ Send email with auto-generated test data
- ✅ Send email with existing athlete
- ✅ Verify email log in database
- ✅ Check Resend delivery status

### Automation Tests
- ✅ Multi-day time progression (Days 1-10)
- ✅ Mock future dates
- ✅ Duplicate prevention (no repeat emails)
- ✅ Healthy vs breached metrics
- ✅ Baseline comparison logic

### Scheduler Tests
- ✅ Manual daily job execution
- ✅ Recovery alert evaluation for all athletes
- ✅ Background scheduler control
- ✅ Job status monitoring

---

## 📋 What the Daily Job Does Now

**Before (old):**
```python
def daily_job():
    result = ingest_recent(days=7)
    print(f"[Daily Job] {datetime.now().isoformat()} -> {result}")
```

**After (new):**
```python
def daily_job():
    # Step 1: Ingest data
    ingest_result = ingest_recent(days=7)
    
    # Step 2: Check ALL athletes for recovery alerts
    athletes = list_athletes()
    for athlete in athletes:
        result = evaluate_recovery_alert(
            athlete_id=athlete.id,
            check_date=get_effective_today(),
            threshold=0.05
        )
        # Sends email if metrics breached
    
    # Returns summary
```

---

## 🎨 Recovery Alert Email Format

**Subject:**
```
Recovery Alert for 2025-10-25
```

**Body:**
```
Multiple recovery indicators breached their baselines.
- Sleep Hours: 6.50 vs baseline 8.00 (18.8% below)
- HRV: 68.00 vs baseline 80.00 (15.0% below)
- Resting HR: 56.00 vs baseline 50.00 (12.0% above)

Recommend checking in with the athlete and adjusting training if necessary.
```

---

## 🛠️ Helper Classes

### LiveTestHelper
For live email testing:
```python
from tests.test_recovery_alerts_live import LiveTestHelper

helper = LiveTestHelper()
athlete_id = helper.create_test_athlete()
helper.create_baseline_metrics(athlete_id, hrv_mean=80, sleep_mean=8, rhr_mean=50)
helper.create_breached_metrics(athlete_id, test_date=date.today())
# ... test code ...
helper.cleanup()  # Always clean up!
```

### AutomationTestHelper
For automation testing:
```python
from tests.test_automation import AutomationTestHelper

helper = AutomationTestHelper()
athlete_id = helper.create_test_athlete()
helper.inject_baseline_data(athlete_id, end_date=date.today())
helper.inject_breached_metrics(athlete_id, metric_date=date.today())
# ... test code ...
helper.cleanup()  # Always clean up!
```

---

## ✅ Verification Checklist

After running live test, verify:

- [ ] Email received at HEAD_COACH_EMAIL address
- [ ] Email contains correct athlete information
- [ ] Metrics show proper baseline comparison
- [ ] Percentage changes calculated correctly
- [ ] Email log created in database
- [ ] Test data cleaned up automatically

---

## 🔍 Troubleshooting Quick Fixes

**No email received?**
```python
from app.utils.settings import settings
print(f"API Key: {settings.resend_api_key[:10]}...")
print(f"Email: {settings.head_coach_email}")
```

**Email says "logged" not "sent"?**
```bash
pip install resend
```

**Alert not triggering?**
```python
# Check if baseline exists
from app.services.baseline import get_baseline
baseline = get_baseline(athlete_id=1, metric_name="hrv", window="monthly")
print(baseline)
```

**Duplicate email issue?**
```python
# Clear email log for testing
from app.data.db import get_session
from app.models.tables import EmailLog
from sqlalchemy import delete

with get_session() as session:
    session.execute(delete(EmailLog).where(EmailLog.athlete_id == 1))
    session.commit()
```

---

## 📊 Test Data Examples

### Healthy Metrics (Won't Trigger)
- HRV: 82 (baseline: 80) ✅
- Sleep: 8.2 hrs (baseline: 8.0) ✅
- RHR: 49 (baseline: 50) ✅

### Breached Metrics (Will Trigger)
- HRV: 68 (baseline: 80) ⚠️ -15%
- Sleep: 6.5 hrs (baseline: 8.0) ⚠️ -18.75%
- RHR: 56 (baseline: 50) ⚠️ +12%

### Threshold
- Default: **5%** deviation
- Adjustable in `evaluate_recovery_alert(threshold=0.05)`

---

## 🎯 Next Steps

1. **Test the live email:**
   ```bash
   python tests/test_recovery_alerts_live.py
   ```

2. **Verify it works with your athletes:**
   ```bash
   python test_automation_helper.py
   # Choose option 2
   ```

3. **Test the scheduler:**
   ```bash
   python test_automation_helper.py
   # Choose option 3
   ```

4. **Read the full guide:**
   - Open `RECOVERY_ALERTS_TESTING_GUIDE.md`

---

## 💡 Pro Tips

1. **Use the interactive helper** - It's the easiest way to test everything
2. **Test with real athletes** - More confidence than synthetic data
3. **Check email logs** - Understand duplicate prevention
4. **Monitor Resend dashboard** - See delivery stats
5. **Run automation tests** - Catch edge cases

---

**Ready to test? Run:**
```bash
python test_automation_helper.py
```

**Questions? See:**
- `RECOVERY_ALERTS_TESTING_GUIDE.md` - Full documentation
- `tests/test_recovery_alerts_live.py` - Live email test code
- `tests/test_automation.py` - Automation test code
- `test_automation_helper.py` - Interactive helper code
