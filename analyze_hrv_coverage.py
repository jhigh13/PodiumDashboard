"""Check HRV data availability and chart requirements."""
from datetime import date, timedelta
from app.data.db import get_session
from app.models.tables import DailyMetric
from sqlalchemy import select

def analyze_hrv_coverage():
    with get_session() as session:
        # Get last 90 days
        end = date.today()
        start = end - timedelta(days=90)
        
        stmt = select(DailyMetric).where(
            DailyMetric.athlete_id == 1,
            DailyMetric.date >= start
        ).order_by(DailyMetric.date.desc())
        
        metrics = session.scalars(stmt).all()

        print(f"Total daily_metrics records found: {len(metrics)}")
        hrv_values = [m.hrv for m in metrics if m.hrv is not None]
        
        hrv_dates = [m.date for m in metrics if m.hrv is not None]
        rhr_dates = [m.date for m in metrics if m.rhr is not None]

        for m in metrics:
            print(f"{m.date} | HRV: {m.hrv or 'NULL'} | RHR: {m.rhr or 'NULL'} | Sleep: {m.sleep_hours or 'NULL'}")
        
        print("=" * 60)
        print("HRV DATA COVERAGE ANALYSIS")
        print("=" * 60)
        print(f"\n📊 Last 90 days: {start} to {end}")
        print(f"Total daily_metrics records: {len(metrics)}")
        print(f"\n✅ RHR data: {len(rhr_dates)}/{len(metrics)} days ({100*len(rhr_dates)/len(metrics):.1f}%)")
        print(f"{'⚠️' if len(hrv_dates) < 7 else '✅'} HRV data: {len(hrv_dates)}/{len(metrics)} days ({100*len(hrv_dates)/len(metrics) if metrics else 0:.1f}%)")
        
        if hrv_dates:
            print(f"\n📅 Dates with HRV data:")
            for d in hrv_dates[:10]:  # Show first 10
                print(f"   - {d}")
            if len(hrv_dates) > 10:
                print(f"   ... and {len(hrv_dates) - 10} more")
        else:
            print(f"\n❌ No HRV data found!")
        
        print("\n" + "=" * 60)
        print("CHART REQUIREMENTS")
        print("=" * 60)
        print(f"Minimum for charts: 7 days of data")
        print(f"Current HRV data: {len(hrv_dates)} days")
        
        if len(hrv_dates) < 7:
            print(f"\n⚠️  Need {7 - len(hrv_dates)} more days of HRV data for charts to display")
            print(f"\n💡 SOLUTIONS:")
            print(f"   1. Log HRV in TrainingPeaks for the next {7 - len(hrv_dates)} days")
            print(f"   2. OR: Backfill HRV data in TrainingPeaks for past dates")
            print(f"   3. OR: Run 'Sync Last 365 Days' if you have older HRV data")
        else:
            print(f"\n✅ You have enough HRV data! Charts should display.")
            print(f"   - 7-day rolling avg needs: 7 days ✅")
            print(f"   - 30-day rolling avg needs: 30 days {'✅' if len(hrv_dates) >= 30 else '⚠️'}")
            print(f"   - 90-day rolling avg needs: 90 days {'✅' if len(hrv_dates) >= 90 else '⚠️'}")
        
        print("\n" + "=" * 60)
        print("ACTION ITEMS")
        print("=" * 60)
        
        if len(hrv_dates) >= 7:
            print("✅ Ready to use charts and baselines!")
            print("   - Refresh dashboard to see HRV chart")
            print("   - Click 'Calculate Baselines' button")
        elif len(hrv_dates) >= 1:
            print("⚠️  HRV ingestion is WORKING but needs more data:")
            print("   1. Log HRV daily in TrainingPeaks")
            print("   2. Run Manual Sync each day")
            print("   3. After 7 days, charts will display")
            print("\n   OR check if you have older HRV data:")
            print("   - Click 'Sync Last 365 Days' to fetch historical data")
        else:
            print("❌ HRV ingestion not working OR no HRV data in TrainingPeaks:")
            print("   1. Verify HRV is visible in TrainingPeaks dashboard")
            print("   2. Check your device is syncing HRV to TrainingPeaks")
            print("   3. Try manual entry in TrainingPeaks as test")

if __name__ == "__main__":
    analyze_hrv_coverage()
