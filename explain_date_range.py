"""Test to show exactly what date range Manual Sync fetches."""
from datetime import date, timedelta

def show_sync_range(days=7):
    """Show what date range will be fetched for a given number of days."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    
    print("=" * 60)
    print("MANUAL SYNC DATE RANGE CALCULATION")
    print("=" * 60)
    print(f"\nRequested days: {days}")
    print(f"Today (end date): {end}")
    print(f"Calculation: end - timedelta(days={days} - 1) = {end} - {days-1} days")
    print(f"Start date: {start}")
    print(f"\n📅 Actual date range fetched: {start} to {end}")
    print(f"   This includes: {(end - start).days + 1} days total")
    
    print(f"\n🗓️  Specific dates that will be queried:")
    current = start
    while current <= end:
        print(f"   - {current}")
        current += timedelta(days=1)
    
    print("\n" + "=" * 60)
    print("EXAMPLES WITH DIFFERENT DAY COUNTS")
    print("=" * 60)
    
    for test_days in [1, 3, 7, 14, 30]:
        test_end = date.today()
        test_start = test_end - timedelta(days=test_days - 1)
        actual_days = (test_end - test_start).days + 1
        print(f"days={test_days}: {test_start} to {test_end} ({actual_days} days)")
    
    print("\n💡 Why 'days - 1'?")
    print("   To make the count inclusive of both start AND end dates.")
    print("   Example: days=7 means 'last 7 days' including today:")
    print("           Oct 2 - 6 days = Sept 26 → fetches Sept 26 through Oct 2")
    print("           That's 7 days: 26,27,28,29,30,1,2 ✅")
    print("\n   If we used 'days' directly:")
    print("           Oct 2 - 7 days = Sept 25 → would fetch Sept 25 through Oct 2")
    print("           That's 8 days! ❌")

if __name__ == "__main__":
    show_sync_range(days=7)
