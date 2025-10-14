from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from app.utils.settings import settings
import pytz
from app.services.ingest import ingest_recent

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
    result = ingest_recent(days=7)
    print(f"[Daily Job] {datetime.now().isoformat()} -> {result}")
