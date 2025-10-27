# Recovery Alerts Testing Guide

This guide explains how to test the recovery alerts feature, including live email sending and automation testing.

## 📋 Table of Contents

1. [Quick Start](#quick-start)
2. [Live Email Testing](#live-email-testing)
3. [Automation Testing](#automation-testing)
4. [Interactive Test Helper](#interactive-test-helper)
5. [Daily Scheduler](#daily-scheduler)
6. [Troubleshooting](#troubleshooting)

---

## Quick Start

### Prerequisites

1. **SendGrid API Key**: Verify it's configured in your `.env` file:
   ```
   SENDGRID_API_KEY=your_api_key_here
   HEAD_COACH_EMAIL=your_email@example.com
   ```

2. **Database**: Ensure your database connection is working

3. **Dependencies**: Make sure all packages are installed:
   ```bash
   pip install -r requirements.txt
   ```

### Fastest Way to Test

Run the interactive helper script:

```bash
python test_automation_helper.py
```

This provides a menu-driven interface for all testing options.

---

## Live Email Testing

### Test 1: Send Live Email with Test Data

This creates a test athlete, sets up baselines, and sends a REAL recovery alert email.

**Command:**
```bash
python tests/test_recovery_alerts_live.py
```

**What it does:**
1. Creates a test athlete
2. Sets up baseline metrics (HRV=80, Sleep=8hrs, RHR=50)
3. Creates breached metrics (HRV=68, Sleep=6.5hrs, RHR=56)
4. Sends a real email to `HEAD_COACH_EMAIL`
5. Cleans up all test data

**Expected Email Content:**
```
Subject: Recovery Alert for 2025-10-25

Multiple recovery indicators breached their baselines.
- Sleep Hours: 6.50 vs baseline 8.00 (18.8% below)
- HRV: 68.00 vs baseline 80.00 (15.0% below)
- Resting HR: 56.00 vs baseline 50.00 (12.0% above)

Recommend checking in with the athlete and adjusting training if necessary.
```

### Test 2: Test with Existing Athlete

If you want to test with an actual athlete in your database:

1. Open `tests/test_recovery_alerts_live.py`
2. Scroll to `test_live_with_existing_athlete()` function
3. Change the `ATHLETE_ID` value
4. Run in Python:

```python
from tests.test_recovery_alerts_live import test_live_with_existing_athlete
test_live_with_existing_athlete()
```

### Using pytest

You can also run these as pytest tests:

```bash
# Run live email test
pytest tests/test_recovery_alerts_live.py::test_live_recovery_alert_email -v

# Note: This will actually send an email!
```

---

## Automation Testing

### Test Scenarios

#### 1. Multi-Day Time Simulation

Tests a complete multi-day scenario:
- Days 1-7: Healthy metrics (no alerts)
- Day 8: Breached metrics (alert triggered, email sent)
- Day 9: Still breached (no email - already sent)
- Day 10: Back to healthy (no alert)

**Command:**
```bash
python tests/test_automation.py
```

Or run specific test:
```python
from tests.test_automation import test_time_simulation_scenario
test_time_simulation_scenario()
```

#### 2. Mock Time Progression

Tests the system with mocked dates to simulate future scenarios.

```python
from tests.test_automation import test_scheduler_with_mock_time
test_scheduler_with_mock_time()
```

#### 3. Manual Daily Job Execution

Runs the complete daily job manually (ingestion + recovery alerts):

```python
from tests.test_automation import test_scheduler_daily_job_manual
test_scheduler_daily_job_manual()
```

### Creating Custom Test Data

Use the `AutomationTestHelper` class to create your own test scenarios:

```python
from tests.test_automation import AutomationTestHelper
from datetime import date, timedelta

helper = AutomationTestHelper()

try:
    # Create test athlete
    athlete_id = helper.create_test_athlete("My Test Athlete")
    
    # Set up baseline
    helper.inject_baseline_data(
        athlete_id=athlete_id,
        end_date=date.today() - timedelta(days=1),
        hrv_mean=75.0,
        sleep_mean=7.5,
        rhr_mean=52.0
    )
    
    # Create breached metrics for today
    helper.inject_breached_metrics(
        athlete_id=athlete_id,
        metric_date=date.today(),
        hrv=63.0,     # ~16% below baseline
        sleep_hours=6.0,  # ~20% below baseline
        rhr=58.0      # ~11.5% above baseline
    )
    
    # Test recovery alert
    from app.services.recovery_alerts import evaluate_recovery_alert
    result = evaluate_recovery_alert(athlete_id=athlete_id, check_date=date.today())
    
    print(f"Triggered: {result['triggered']}")
    print(f"Reason: {result['reason']}")
    
finally:
    # Clean up
    helper.cleanup()
```

---

## Interactive Test Helper

The `test_automation_helper.py` script provides an easy-to-use menu interface for all testing operations.

### Running the Helper

```bash
python test_automation_helper.py
```

### Menu Options

```
📧 LIVE EMAIL TESTS:
  1. Send live recovery alert email (creates test data)
  2. Send live email with existing athlete

⏰ SCHEDULER TESTS:
  3. Run daily job manually (right now)
  4. Test multi-day time simulation
  5. Test with mocked time progression

🔧 MANUAL OPERATIONS:
  6. List all athletes
  7. Check recovery alert for specific athlete
  8. Create test data for specific date

⚙️  SCHEDULER CONTROL:
  9. Start background scheduler
 10. Check scheduler status
```

### Example Workflows

#### Workflow 1: Test Email for Real Athlete

1. Run: `python test_automation_helper.py`
2. Choose option `6` to list athletes
3. Note the athlete ID you want to test
4. Choose option `2` to send live email
5. Enter the athlete ID
6. Confirm to send

#### Workflow 2: Create Custom Test Scenario

1. Run: `python test_automation_helper.py`
2. Choose option `8` to create test data
3. Create new athlete or select existing
4. Choose date for metrics
5. Select metric type (healthy/breached/custom)
6. Use option `7` to check recovery alert

#### Workflow 3: Test Daily Scheduler

1. Run: `python test_automation_helper.py`
2. Choose option `3` to run daily job manually
3. Review output for ingestion and alert results

---

## Daily Scheduler

### How It Works

The scheduler runs automatically at the configured time (`DAILY_JOB_TIME` in `.env`, default: 07:30 AM Mountain Time).

**Daily Job Steps:**
1. Ingest recent data from TrainingPeaks (last 7 days)
2. Evaluate recovery alerts for ALL athletes in the database
3. Send email alerts for athletes with breached metrics
4. Log all email sends to prevent duplicates

### Scheduler Configuration

Edit `.env` file:
```
DAILY_JOB_TIME=07:30  # 24-hour format HH:MM
```

### Starting the Scheduler

The scheduler starts automatically when you run your main application:

```python
from app.scheduling.scheduler import start_scheduler
start_scheduler()
```

Or start it manually for testing:

```bash
python test_automation_helper.py
# Choose option 9: Start background scheduler
```

### Checking Scheduler Status

```bash
python test_automation_helper.py
# Choose option 10: Check scheduler status
```

### Manual Trigger

To manually run the daily job without waiting for the scheduled time:

```python
from app.scheduling.scheduler import daily_job
result = daily_job()
print(result)
```

### Expected Output

When the daily job runs:

```
============================================================
[Daily Job Started] 2025-10-25T07:30:00.123456
============================================================

[1/2] Ingesting recent data from TrainingPeaks...
  ✓ Ingestion result: {'status': 'success', ...}

[2/2] Evaluating recovery alerts for all athletes...
  Found 3 athlete(s)
  🚨 Alert triggered for John Doe (ID: 1)
     Reason: sleep_and_hrv_rhr_breach
     Email status: sent
  ✓ Jane Smith (ID: 2): conditions_not_met
  ✓ Bob Johnson (ID: 3): insufficient_baseline_or_metric

  Summary: 1 alert(s) triggered out of 3 athlete(s)

============================================================
[Daily Job Completed] 2025-10-25T07:30:15.789012
============================================================
```

---

## Troubleshooting

### Email Not Sending

**Problem:** Test runs but no email received

**Solutions:**
1. Check SendGrid API key is valid:
   ```python
   from app.utils.settings import settings
   print(settings.sendgrid_api_key)
   ```

2. Verify HEAD_COACH_EMAIL is correct:
   ```python
   from app.utils.settings import settings
   print(settings.head_coach_email)
   ```

3. Check SendGrid dashboard for delivery issues
4. Check spam/junk folder

### Email Says "logged" Instead of "sent"

**Problem:** `email_status` shows "logged" in test output

**Cause:** SendGrid package not installed or API key missing

**Solution:**
```bash
pip install sendgrid
```

### Duplicate Email Prevention

**Problem:** Not receiving email even though metrics are breached

**Cause:** Email already sent for this athlete/date combination

**Solution:** Check the email log:
```python
from app.data.db import get_session
from app.models.tables import EmailLog
from sqlalchemy import select

with get_session() as session:
    logs = session.execute(select(EmailLog)).scalars().all()
    for log in logs:
        print(f"{log.athlete_id} | {log.date} | {log.email_type} | {log.status}")
```

To reset for testing, delete the log entry:
```python
from app.data.db import get_session
from app.models.tables import EmailLog
from sqlalchemy import delete
from datetime import date

with get_session() as session:
    session.execute(
        delete(EmailLog).where(
            EmailLog.athlete_id == 1,
            EmailLog.date == date.today()
        )
    )
    session.commit()
```

### No Baseline Data

**Problem:** Alert shows "insufficient_baseline_or_metric"

**Cause:** No baseline metrics exist for the athlete

**Solution:** Create baselines manually:
```python
from tests.test_automation import AutomationTestHelper
from datetime import date, timedelta

helper = AutomationTestHelper()
helper.inject_baseline_data(
    athlete_id=1,  # Your athlete ID
    end_date=date.today() - timedelta(days=1),
    hrv_mean=75.0,
    sleep_mean=7.5,
    rhr_mean=52.0
)
```

Or run the baseline calculation service (if you have it implemented):
```python
from app.services.baseline import calculate_baselines
calculate_baselines(athlete_id=1)
```

### Scheduler Not Running

**Problem:** Daily job never executes automatically

**Solutions:**
1. Verify scheduler is started:
   ```python
   from app.scheduling.scheduler import scheduler
   print(f"Running: {scheduler.running}")
   ```

2. Check scheduled time:
   ```python
   from app.utils.settings import settings
   print(f"Scheduled: {settings.daily_job_time}")
   ```

3. Ensure application stays running (scheduler works in background)

4. Check timezone is correct (America/Denver)

### Test Data Cleanup Issues

**Problem:** Test data remains in database after test

**Cause:** Exception occurred before cleanup

**Solution:** Manually clean up:
```python
from tests.test_automation import AutomationTestHelper
helper = AutomationTestHelper()
helper.test_athlete_id = ATHLETE_ID_TO_DELETE
helper.cleanup()
```

---

## Best Practices

### For Development

1. **Always use test helpers** - They handle cleanup automatically
2. **Check email logs** - Prevents confusion about duplicate sends
3. **Use meaningful test athlete names** - Easy to identify in database
4. **Test incrementally** - Start with unit tests, then integration tests

### For Production

1. **Monitor email delivery** - Check SendGrid dashboard regularly
2. **Review alert frequency** - Adjust threshold (default 5%) if needed
3. **Verify scheduler uptime** - Ensure application stays running
4. **Test after configuration changes** - Run integration tests after .env updates

### Email Testing Checklist

Before going live:
- [ ] SendGrid API key is valid and production-ready
- [ ] HEAD_COACH_EMAIL is correct
- [ ] Test email received successfully
- [ ] Email content is clear and actionable
- [ ] Baseline calculations are accurate
- [ ] Alert logic tested with various scenarios
- [ ] Duplicate prevention works correctly
- [ ] Scheduler runs at expected time

---

## Additional Resources

### Files Created

- `tests/test_recovery_alerts_live.py` - Live email integration tests
- `tests/test_automation.py` - Automation and time simulation tests
- `test_automation_helper.py` - Interactive test helper script
- `app/scheduling/scheduler.py` - Updated with recovery alert evaluation

### Key Functions

**Recovery Alerts:**
- `evaluate_recovery_alert()` - Main alert evaluation function
- `_get_metric_status()` - Check if metric breached baseline
- `_select_baseline()` - Find appropriate baseline for metric

**Testing:**
- `LiveTestHelper` - Create test data for email tests
- `AutomationTestHelper` - Create test data for automation tests
- `daily_job()` - Manual trigger for scheduled job

### Configuration

Key environment variables:
```
SENDGRID_API_KEY=your_key
HEAD_COACH_EMAIL=coach@example.com
DAILY_JOB_TIME=07:30
DATABASE_URL=postgresql://...
```

---

## Support

If you encounter issues not covered in this guide:

1. Check application logs
2. Verify database connectivity
3. Test SendGrid API key separately
4. Review TrainingPeaks API status
5. Check error messages in console output

For feature requests or bugs, document:
- Steps to reproduce
- Expected vs actual behavior
- Error messages
- Environment details (.env configuration)

---

**Last Updated:** October 25, 2025
**Version:** 1.0
