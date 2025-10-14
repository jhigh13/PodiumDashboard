"""Test script to verify HRV field mapping fix."""
import sys
from datetime import date, timedelta
from app.services.ingest import ingest_recent

def test_hrv_ingestion():
    print("Testing HRV ingestion with fixed field mapping...")
    print("=" * 60)
    
    try:
        # Run a sync for last 7 days
        result = ingest_recent(days=7)
        
        print(f"\n✅ Sync completed successfully!")
        print(f"Workouts: {result.get('workouts_inserted', 0)} new, {result.get('workout_duplicates', 0)} duplicates")
        print(f"Metrics: {result.get('metrics_saved', 0)} saved from {result.get('metrics_fetched', 0)} fetched")
        
        if 'metric_field_names' in result:
            print(f"\nAPI Fields: {', '.join(result['metric_field_names'][:10])}...")
        
        # Now check the database
        from app.data.db import get_session
        from app.models.tables import DailyMetric
        from sqlalchemy import select
        
        with get_session() as session:
            stmt = select(DailyMetric).where(
                DailyMetric.athlete_id == 1
            ).order_by(DailyMetric.date.desc()).limit(10)
            
            metrics = session.scalars(stmt).all()
            
            print("\n" + "=" * 60)
            print("Recent metrics in database:")
            print("=" * 60)
            
            hrv_count = 0
            for m in metrics:
                hrv_status = "✅" if m.hrv else "❌"
                print(f"{m.date} | HRV: {m.hrv or 'NULL'} {hrv_status} | RHR: {m.rhr or 'NULL'} | Sleep: {m.sleep_hours or 'NULL'}")
                if m.hrv:
                    hrv_count += 1
            
            print("\n" + "=" * 60)
            if hrv_count > 0:
                print(f"✅ SUCCESS! Found {hrv_count} records with HRV data!")
            else:
                print(f"❌ PROBLEM: No HRV data found in database")
                print("   Check if TrainingPeaks actually has HRV logged for these dates")
    
    except Exception as e:
        print(f"\n❌ Error during sync: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_hrv_ingestion()
