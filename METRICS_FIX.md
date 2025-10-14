# Daily Metrics Ingestion Fix

## Problem
Dashboard wasn't displaying daily metrics even though they were visible in TrainingPeaks app.

## Root Causes Identified

### 1. Wrong API Field Names
**Issue**: Code was looking for camelCase field names like `restingHeartRate`, but TrainingPeaks API uses PascalCase like `Pulse`, `HRV`, `SleepHours`.

**From API Docs** (https://github.com/TrainingPeaks/PartnersAPI/wiki/v2-Metrics-Get-Athlete-Metrics):
```json
{
  "MetricId": "...",
  "AthleteId": 123456,
  "DateTime": "2022-06-01T06:12:34",
  "WeightInKilograms": 68.1,
  "HRV": 84.1,
  "Steps": 12345,
  "Stress": "Low",
  "SleepQuality": "Good",
  "SleepHours": 8,
  "Pulse": 45
}
```

### 2. Only Storing One Metric
**Issue**: Code was only picking the "latest" metric from the date range instead of storing all metrics.

**Problem**: API returns an array of metric entries, each with different fields. You might have multiple metrics per day (e.g., morning HRV, evening weight).

### 3. Dashboard Showing Only Latest
**Issue**: Dashboard query was `limit(1)` instead of showing all metrics in the date range.

## Changes Made

### 1. `app/services/ingest.py` - Field Name Mapping
**Before:**
```python
rhr=latest.get('restingHeartRate'),  # Wrong!
hrv=latest.get('hrv'),  # Wrong!
sleep_hours=latest.get('sleepHours'),  # Wrong!
```

**After:**
```python
rhr=m.get('Pulse') or m.get('RestingHeartRate') or m.get('restingHeartRate'),
hrv=m.get('HRV') or m.get('hrv'),
sleep_hours=m.get('SleepHours') or m.get('sleepHours'),
```

Now tries multiple field name variations to be resilient.

### 2. `app/services/ingest.py` - Process All Metrics
**Before:** Picked one "latest" metric
**After:** Loops through all metrics in the array and stores each one

```python
for m in metrics:
    metric_date = _coerce_date(m.get('DateTime'))
    # Store each metric entry
    dm = DailyMetric(...)
    session.add(dm)
```

### 3. `app/services/ingest.py` - Debug Information
Added diagnostic fields to return value:
- `metrics_raw_sample`: First metric from API for inspection
- `metric_field_names`: All field names found in API response
- `metrics_saved`: Count of metrics actually stored

### 4. `app/ui/dashboard_view.py` - Show All Metrics
**Before:** Showed only 1 metric (latest)
**After:** 
- Shows latest metric in summary cards
- Shows table of all metrics in date range
- Better formatting with "—" for null values

### 5. `app/ui/dashboard_view.py` - Sync Details
Added expandable panel showing:
- Metrics fetched vs saved
- Actual API field names received
- Raw sample of first metric from API
- Date range synced

## TrainingPeaks Metrics API Details

### Available Fields (per API docs)
- `MetricId`: Unique ID
- `AthleteId`: TrainingPeaks athlete ID
- `DateTime`: Local time without timezone
- `UploadClient`: Source app
- `Pulse` / `RestingHeartRate`: RHR
- `HRV`: Heart rate variability
- `SleepHours`: Sleep duration
- `SleepQuality`: Qualitative sleep
- `WeightInKilograms`: Body weight
- `Steps`: Step count
- `Stress`: Stress level
- `CTL`: Chronic Training Load (fitness)
- `ATL`: Acute Training Load (fatigue)
- `TSB`: Training Stress Balance (form)

**Important**: Not every field will be returned for every metric. Fields depend on what data the athlete has logged.

## Testing Instructions

### Step 1: Restart Streamlit
```powershell
streamlit run app/main.py
```

### Step 2: Manual Sync
1. Go to Dashboard
2. Click "Manual Sync"
3. Watch for "🔍 Sync Details" expander

### Step 3: Check Sync Details
Look for:
- **Metrics fetched**: Should be > 0 if you have metrics in TrainingPeaks
- **API Metric Fields**: Shows actual field names from TrainingPeaks
- **Raw JSON**: First metric sample

Example output:
```json
{
  "MetricId": "ABC123...",
  "AthleteId": 123456,
  "DateTime": "2025-10-01T07:30:00",
  "Pulse": 52,
  "HRV": 78.5,
  "SleepHours": 7.5
}
```

### Step 4: View Dashboard
- **Summary cards**: Shows most recent values
- **Metrics table**: Shows all entries in date range

## Expected Results

### If You Have Metrics in TrainingPeaks:
✅ Sync Details shows `metrics_fetched > 0`
✅ Dashboard shows values in metric cards
✅ Table shows multiple metric entries if available

### If Still No Metrics:
Check Sync Details for:
1. **`metrics_fetched: 0`** → No metrics in date range or API issue
2. **`metric_field_names: []`** → Empty response from API
3. **Error message** → Permissions or API endpoint issue

## Common Scenarios

### Scenario 1: API Returns Empty Array
**Cause**: No metrics logged in TrainingPeaks for that date range
**Solution**: Log some metrics in TrainingPeaks sandbox, then sync again

### Scenario 2: Metrics Fetched but Not Saved
**Cause**: DateTime field might be null or unparseable
**Solution**: Check raw sample JSON in Sync Details

### Scenario 3: Some Fields Always Null
**Cause**: TrainingPeaks only returns fields that athlete has logged
**Solution**: Normal behavior - dashboard shows "—" for missing values

## Next Steps (Optional Enhancements)

1. **Charts**: Add line charts for CTL/ATL/TSB trends
2. **Date Picker**: Allow selecting specific date ranges
3. **Metric Entry**: Add UI to POST new metrics via API
4. **Aggregations**: Calculate weekly/monthly averages
5. **Alerts**: Highlight concerning patterns (high ATL, low HRV)

## TrainingPeaks API Endpoints Used

### For Metrics by Date Range:
```
GET /v2/metrics/{start}/{end}
GET /v2/metrics/{athleteId}/{start}/{end}
```

Our code tries both:
1. Without athlete ID (uses token's athlete)
2. With athlete ID (if known from profile)

---
**Date Fixed:** October 1, 2025
**Issue:** Metrics not displaying despite being in TrainingPeaks
**Solution:** Fixed field name mapping and process all metric entries
