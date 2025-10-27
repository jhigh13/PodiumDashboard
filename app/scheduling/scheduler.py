from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date
from app.utils.settings import settings
import pytz
from app.services.ingest import ingest_recent
from app.services.athletes import list_athletes
from app.services.recovery_alerts import evaluate_recovery_alert
from app.utils.dates import get_effective_today

scheduler = BackgroundScheduler(timezone=pytz.timezone('America/Denver'))

_job_started = False

def start_scheduler():
    global _job_started
    if not scheduler.running:
        scheduler.start()
    if not _job_started:
        hour, minute = map(int, settings.daily_job_time.split(':'))
        scheduler.add_job(daily_job, 'cron', hour=hour, minute=minute, id='daily_job', replace_existing=True)
        _job_started = True


def daily_job():
    """
    Daily automated job that runs at the scheduled time.
    
    This job:
    1. Ingests recent data from TrainingPeaks (21 days to account for sandbox offset)
    2. Evaluates recovery alerts for all athletes
    
    Note: Uses SANDBOX_CURRENT_DAY_OFFSET to handle sandbox environment's date lag.
    When offset=10, "today" is treated as 10 days ago. Set to 0 for production.
    """
    timestamp = datetime.now().isoformat()
    print(f"\n{'='*60}")
    print(f"[Daily Job Started] {timestamp}")
    print(f"{'='*60}")
    
    # Show sandbox offset information
    offset = settings.sandbox_current_day_offset
    effective_today = get_effective_today()
    actual_today = date.today()
    
    print(f"\n📅 Date Configuration:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Ingesting: 21 days (ensures full coverage with offset)")
    
    # Step 1: Ingest recent data (21 days to ensure we have enough historical data)
    print("\n[1/2] Ingesting recent data from TrainingPeaks...")
    ingest_result = ingest_recent(days=21)
    print(f"  ✓ Ingestion result: {ingest_result}")
    
    # Step 2: Evaluate recovery alerts for all athletes
    print("\n[2/2] Evaluating recovery alerts for all athletes...")
    athletes = list_athletes()
    check_date = get_effective_today()
    
    if not athletes:
        print("  ℹ No athletes found in database")
    else:
        print(f"  Found {len(athletes)} athlete(s)")
        
        alert_count = 0
        for athlete in athletes:
            try:
                result = evaluate_recovery_alert(
                    athlete_id=athlete.id,
                    check_date=check_date,
                    threshold=0.05,  # 5% threshold
                )
                
                if result['triggered']:
                    alert_count += 1
                    print(f"  🚨 Alert triggered for {athlete.name} (ID: {athlete.id})")
                    print(f"     Reason: {result['reason']}")
                    print(f"     Email status: {result.get('email_status', 'N/A')}")
                else:
                    print(f"  ✓ {athlete.name} (ID: {athlete.id}): {result['reason']}")
                    
            except Exception as e:
                print(f"  ✗ Error evaluating {athlete.name} (ID: {athlete.id}): {e}")
        
        print(f"\n  Summary: {alert_count} alert(s) triggered out of {len(athletes)} athlete(s)")
    
    print(f"\n{'='*60}")
    print(f"[Daily Job Completed] {datetime.now().isoformat()}")
    print(f"{'='*60}\n")
    
    return {
        "timestamp": timestamp,
        "ingest_result": ingest_result,
        "athletes_evaluated": len(athletes) if athletes else 0,
        "alerts_triggered": alert_count if athletes else 0,
    }
