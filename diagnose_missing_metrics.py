from datetime import date
from app.services.diagnostics import find_missing_dates

if __name__ == "__main__":
    report = find_missing_dates(days_back=20)
    print("Date Window:", report['start'], '->', report['end'])
    print(f"Expected {report['expected_days']} days, Present {report['present_days']}, Missing {report['missing_count']}")
    if report['missing_dates']:
        print("Missing dates:")
        for d in report['missing_dates']:
            print("  -", d)
    else:
        print("No missing dates ✅")
    print("\nAPI Probe Results for Missing Ranges:")
    for probe in report['api_probe']:
        print(f"  Range {probe['range']} -> API returned {probe['count']} records")
        if probe['sample']:
            print("   Sample:", probe['sample'][0])
