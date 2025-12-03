from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date
from app.utils.settings import settings
import pytz
from app.services.ingest import ingest_recent
from app.services.athletes import list_athletes
from app.services.recovery_alerts import evaluate_recovery_alert
from app.services.compliance import get_compliance_for_day
from app.services.llm import llm_client
from app.services.email import email_client
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
    1. Ingests the latest day's data from TrainingPeaks.
    2. Evaluates recovery alerts (without sending default emails).
    3. Checks workout compliance.
    4. Generates a daily summary using LLM.
    5. Sends the summary email.
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
    print(f"  Ingesting: 1 day (latest)")
    
    # Step 1: Ingest recent data (1 day)
    print("\n[1/3] Ingesting latest data from TrainingPeaks...")
    try:
        ingest_result = ingest_recent(days=1)
        print(f"  ✓ Ingestion result: {ingest_result}")
    except Exception as e:
        print(f"  ✗ Ingestion failed: {e}")
        ingest_result = {"error": str(e)}
    
    # Step 2: Evaluate and Process for each athlete
    print("\n[2/3] Processing athletes (Recovery + Compliance + LLM)...")
    athletes = list_athletes()
    
    if not athletes:
        print("  ℹ No athletes found in database")
    else:
        print(f"  Found {len(athletes)} athlete(s)")
        
        for athlete in athletes:
            print(f"\n  👤 Processing: {athlete.name} (ID: {athlete.id})")
            try:
                # A. Recovery Alert (suppress default email)
                recovery_result = evaluate_recovery_alert(
                    athlete_id=athlete.id,
                    check_date=effective_today,
                    threshold=0.05,
                    send_email=False 
                )
                print(f"     Recovery Triggered: {recovery_result.get('triggered')}")

                # B. Workout Compliance
                compliance_result = get_compliance_for_day(athlete.id, effective_today)
                score = compliance_result.get('records', [{}])[0].get('overall_score') if compliance_result and compliance_result.get('records') else None
                print(f"     Compliance Score: {score}")

                # C. Generate LLM Summary
                print("     Generating AI summary...")
                email_body = llm_client.generate_daily_summary(
                    athlete_name=athlete.name,
                    date_str=effective_today.isoformat(),
                    recovery_data=recovery_result,
                    compliance_data=compliance_result
                )
                
                # D. Send Email
                subject = f"Daily Summary: {athlete.name} - {effective_today.isoformat()}"
                print(f"     Sending email to {settings.head_coach_email}...")
                send_result = email_client.send_daily_summary(
                    to_email=settings.head_coach_email,
                    subject=subject,
                    body=email_body
                )
                print(f"     ✓ Email sent: {send_result.get('status')}")

            except Exception as e:
                print(f"  ✗ Error processing {athlete.name}: {e}")
    
    print(f"\n{'='*60}")
    print(f"[Daily Job Completed] {datetime.now().isoformat()}")
    print(f"{'='*60}\n")
    
    return {
        "timestamp": timestamp,
        "ingest_result": ingest_result,
        "athletes_evaluated": len(athletes) if athletes else 0,
    }
