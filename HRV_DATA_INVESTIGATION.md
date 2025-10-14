# HRV Data Investigation - October 2, 2025

## 🔍 Diagnostic Results

### Database Status
```
Total metrics in last 90 days: 78 records
HRV data:   0/78 (0.0%)   ❌ ALL NULL
RHR data:   77/78 (98.7%) ✅ Nearly complete  
Sleep data: 74/78 (94.9%) ✅ Nearly complete
```

### Root Cause
**The TrainingPeaks API is not returning HRV values.**

Your database has:
- ✅ Excellent RHR coverage (resting heart rate)
- ✅ Excellent Sleep coverage  
- ❌ **Zero HRV data** (all NULL)

This means:
1. Either HRV is not being logged in TrainingPeaks
2. Or it's logged but not being synced from your device
3. Or it's in a different API endpoint we're not querying

## 🔧 Solutions to Get HRV Data

### Option 1: Check TrainingPeaks Data Entry
**In the TrainingPeaks app/website:**
1. Go to your daily metrics view
2. Check if HRV values are showing there
3. If YES → TrainingPeaks has it, we need to fix API query
4. If NO → Need to start logging HRV

### Option 2: Log HRV Data in TrainingPeaks
**If you have a device that tracks HRV:**
- Garmin watches: Sync in Garmin Connect, ensure TP integration enabled
- Whoop: Check Whoop → TrainingPeaks integration settings
- Apple Watch: Use an app that exports to TrainingPeaks
- Manual entry: Enter HRV values directly in TrainingPeaks daily metrics

### Option 3: Verify API Field Names
**Run a manual sync and check the "Sync Details" expander:**
1. In dashboard, click "Manual Sync"
2. Expand "🔍 Sync Details"  
3. Look at the "API Metric Fields" list
4. Check if any field contains "hrv", "HRV", "variability", etc.
5. Share those field names if you see anything related

### Option 4: Check Different API Endpoint
TrainingPeaks might have HRV in a different endpoint. Let me check the API docs for wellness metrics endpoints.

## 📊 What's Working Now

### Charts
- ✅ **RHR chart displays perfectly** (you mentioned seeing the heart rate lines)
- ❌ **HRV chart is empty** (because no data)
- ✅ **Sleep overlay works** (purple dashed line visible)

### Dashboard Improvements Added
1. ✅ **Baseline calculation now shows results table** with mean, std dev, and sample count
2. ✅ **Chart debug expander** shows data availability
3. ✅ **Warnings appear** if HRV/RHR data missing in last 90 days

## 🎯 Next Steps

### Immediate Action
1. **Check your TrainingPeaks account:**
   - Log in to https://home.trainingpeaks.com
   - Go to Dashboard → Metrics
   - Look for HRV values - are they there?

2. **Run Manual Sync and check API response:**
   - In your dashboard, click "Manual Sync"
   - Open "🔍 Sync Details" expander
   - Look at "API Metric Fields" - paste that list here
   - Check the JSON sample - does it have any HRV-related field?

3. **Check the TrainingPeaks app on your phone/device:**
   - Are you logging HRV daily?
   - Is it showing up in the app?

### If HRV Exists in TrainingPeaks
If you confirm HRV data exists in TrainingPeaks but isn't coming through:
- We may need to query a different API endpoint
- The field name might be different (e.g., "HeartRateVariability" instead of "HRV")
- It might require a different scope in OAuth permissions

### If HRV Doesn't Exist
If you haven't been tracking HRV:
- Start logging it daily (manually or via device)
- Wait a week to accumulate 7+ days of data
- Then the HRV chart will populate

## 📈 What the Baseline Calculation Will Show

Now when you click "Calculate Baselines", you'll see a table like this:

```
RHR
Window      Mean    Std Dev   Samples
Annual      37.45   2.13      312
Monthly     36.82   1.87      28  
Weekly      38.14   1.45      7

SLEEP_HOURS
Window      Mean    Std Dev   Samples
Annual      7.63    1.02      298
Monthly     7.89    0.94      27
Weekly      7.45    0.78      7
```

(HRV will appear once you have data)

## 🔍 Diagnostic Script

Created `check_hrv_data.py` to quickly check data availability:
```bash
python check_hrv_data.py
```

This shows:
- Total records
- Date range  
- Recent 10 metrics with actual values
- Percentage of non-null data for each metric

## 💡 Why This Matters

HRV is arguably the **most important recovery metric** because:
- Shows autonomic nervous system balance
- Best predictor of readiness to train hard
- Sensitive to stress, illness, overtraining
- Changes before other metrics (leading indicator)

RHR and Sleep are excellent, but HRV completes the picture!

---

## Summary
- ✅ Dashboard improvements deployed (baseline results display, chart debugging)
- ✅ RHR and Sleep charts working perfectly
- ❌ HRV data is missing from database (API not returning it)
- 🔄 Need to investigate why HRV isn't in TrainingPeaks data

**Next: Please run a Manual Sync and share the "API Metric Fields" list from the Sync Details expander so we can see what fields TrainingPeaks is actually sending.**
