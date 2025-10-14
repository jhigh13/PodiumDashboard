# HRV Field Mapping Fix - October 2, 2025

## 🐛 Bug Found and Fixed

### The Problem
HRV data was **not being saved** to the database despite TrainingPeaks API returning it.

### Root Cause
**Case-sensitivity mismatch in field name mapping!**

TrainingPeaks API returns: `"Hrv": 104` (PascalCase: capital H, lowercase rv)
Our code was checking for: `"HRV"` or `"hrv"` (all uppercase or all lowercase)

Since Python dictionary `.get()` is case-sensitive, it never found the field!

### The Fix
Changed field mapping to check `'Hrv'` **first**:

**Before:**
```python
hrv=m.get('HRV') or m.get('hrv')
```

**After:**
```python
hrv=m.get('Hrv') or m.get('HRV') or m.get('hrv')  # Fixed: API uses 'Hrv' (PascalCase)
```

### Files Changed
1. `app/services/ingest.py` - Line 150 (recent sync function)
2. `app/services/ingest.py` - Line 289 (historical sync function)

## 📊 API Response Analysis

From your Manual Sync output, TrainingPeaks returns **48 fields** including:

**Recovery Metrics (what we care about):**
- ✅ `Hrv: 104` - Heart Rate Variability
- ✅ `Pulse: 36` - Resting Heart Rate (we map this to `rhr`)
- ✅ `SleepHours: 8.84` - Total sleep duration
- ✅ `TimeInDeepSleep: 0.73` - Deep sleep hours
- ✅ `TimeInLightSleep: 5.98` - Light sleep hours
- ✅ `TimeInRemSleep: 2.12` - REM sleep hours
- ✅ `TotalTimeAwake: 0.38` - Awake time during sleep

**Other Available Metrics:**
- Appetite, BloodGlucose, Bmi, Bmr, Diastolic/Systolic (blood pressure)
- Fatigue, Injury, Menstruation, Mood, Motivation, OverallFeeling
- MuscleMass, PercentFat, WeightInKilograms, Spo2
- Steps, Stress, Soreness, Sickness, UrineColor
- YesterdaysTraining, NumberTimesWoken, SleepQuality

## 🎯 How to Get Your Historical HRV Data

### Step 1: Run Manual Sync (to test fix)
1. Go to dashboard
2. Click "Manual Sync" button
3. Check the metrics summary - should now save HRV values
4. Run diagnostic script to verify:
   ```bash
   python check_hrv_data.py
   ```

### Step 2: Run Historical Sync (to populate all past data)
1. In dashboard sidebar, click **"Sync Last 365 Days"**
2. Wait for progress bar to complete (~12 chunks)
3. This will retroactively fetch all your HRV data from TrainingPeaks

### Step 3: Verify Data
Run the diagnostic script again:
```bash
python check_hrv_data.py
```

Should now show:
```
HRV: 312/365 (85.5%)  ✅ (or whatever % you have logged)
```

### Step 4: View HRV Charts
1. Refresh dashboard page
2. Scroll to "📈 Recovery Metrics Trends"
3. HRV chart should now display with all 4 rolling average lines!

### Step 5: Calculate Baselines
1. Click "Calculate Baselines" button
2. View the results table showing HRV baseline statistics
3. Check for any deviation alerts

## 🔍 Why This Happened

TrainingPeaks API documentation is inconsistent:
- Some docs show `HRV` (all caps)
- Actual API returns `Hrv` (PascalCase)
- We coded for the documented version, not the actual version

**Lesson learned:** Always check actual API responses, not just documentation!

## 📈 What You'll See After Fix

### Before (Current State)
```
HRV data: 0/78 (0.0%) ❌
```

### After Manual Sync
```
HRV data: 1/78 (1.3%) ✅ (today's value)
```

### After Historical Sync
```
HRV data: 312/365 (85.5%) ✅ (all your logged values)
```

## 🎉 Expected Results

### HRV Chart Will Show:
- 🔵 365-day rolling average (annual baseline)
- 🔵 90-day rolling average (quarterly trend)
- 🔵 30-day rolling average (monthly trend)
- 🔵 7-day rolling average (weekly acute state)
- 🟣 Weekly sleep overlay (purple dashed line)

### Baseline Calculations Will Include:
```
HRV
Window      Mean     Std Dev   Samples
Annual      89.3     12.4      312
Monthly     91.7     8.2       28
Weekly      104.0    6.1       7
```

### Alerts May Trigger:
If your recent HRV is significantly different from baseline:
- 🟢 Green: Within 0.5 standard deviations (normal)
- 🟡 Yellow: 0.5-1.0 std deviations (monitor)
- 🔴 Red: >1.0 std deviations (investigate)

## 🚀 Next Steps

1. **Immediate:** Run Manual Sync to test the fix
2. **Then:** Run Historical Sync (365 days) to populate all data
3. **Verify:** Check diagnostic script shows HRV data
4. **View:** See HRV chart populate with rolling averages
5. **Calculate:** Click "Calculate Baselines" to see HRV statistics
6. **Monitor:** Watch for deviation alerts in your daily sync

## 📝 Related Issues Fixed

This same PascalCase pattern likely affects other fields. If we want to add more metrics in the future, we now know to check for:
- `PascalCase` (e.g., `Hrv`, `SleepHours`, `OverallFeeling`)
- Not just `ALL_CAPS` or `lowercase`

## 🔧 Other Potential Enhancements

Since the API returns rich sleep data, we could enhance the dashboard with:
- Deep sleep, Light sleep, REM sleep tracking
- Sleep stage breakdown charts
- Sleep quality score
- Time awake during night

And many other wellness metrics:
- Mood, Motivation, OverallFeeling (subjective wellness)
- Stress, Soreness, Fatigue (training load indicators)
- Weight, Body Fat % (body composition trends)

But for now, let's get HRV working first! 🎯

---

**Fix Applied:** October 2, 2025, 11:45 PM
**Issue:** HRV field name case-sensitivity (`Hrv` vs `HRV`)
**Status:** ✅ Fixed in both sync functions
**Next:** Run Manual Sync + Historical Sync to populate data
