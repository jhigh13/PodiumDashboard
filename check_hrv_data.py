"""Quick diagnostic script to check HRV data in database."""
from datetime import date, timedelta
from sqlalchemy import select, func
from app.data.db import get_session
from app.models.tables import DailyMetric

def check_hrv_data():
    with get_session() as session:
        # Get last 90 days
        end = date.today()
        start = end - timedelta(days=90)
        
        stmt = select(DailyMetric).where(
            DailyMetric.athlete_id == 1,
            DailyMetric.date >= start
        ).order_by(DailyMetric.date.desc())
        
        metrics = session.scalars(stmt).all()
        
        print(f"Total metrics found: {len(metrics)}")
        print(f"Date range: {start} to {end}")
        print("\n" + "="*60)
        print("Recent metrics:")
        print("="*60)
        
        for m in metrics[:10]:
            print(f"{m.date} | HRV: {m.hrv or 'NULL'} | RHR: {m.rhr or 'NULL'} | Sleep: {m.sleep_hours or 'NULL'}")
        
        # Count non-null values
        hrv_count = sum(1 for m in metrics if m.hrv is not None)
        rhr_count = sum(1 for m in metrics if m.rhr is not None)
        sleep_count = sum(1 for m in metrics if m.sleep_hours is not None)
        
        print("\n" + "="*60)
        print("Data availability:")
        print("="*60)
        print(f"HRV: {hrv_count}/{len(metrics)} ({100*hrv_count/len(metrics) if metrics else 0:.1f}%)")
        print(f"RHR: {rhr_count}/{len(metrics)} ({100*rhr_count/len(metrics) if metrics else 0:.1f}%)")
        print(f"Sleep: {sleep_count}/{len(metrics)} ({100*sleep_count/len(metrics) if metrics else 0:.1f}%)")
        
        if hrv_count > 0:
            hrv_values = [m.hrv for m in metrics if m.hrv is not None]
            print(f"\nHRV range: {min(hrv_values):.1f} - {max(hrv_values):.1f}")
            print(f"HRV average: {sum(hrv_values)/len(hrv_values):.1f}")

if __name__ == "__main__":
    check_hrv_data()
