# Recovery Metrics Trend Charts - Implementation Summary

## ✅ What's Been Added

### 📈 Two Dual-Axis Interactive Charts

#### Chart 1: HRV Rolling Averages with Weekly Sleep
**Left Y-Axis (HRV in milliseconds):**
- 🔵 **365-day rolling average** - Dark blue, thickest line (annual baseline)
- 🔵 **90-day rolling average** - Medium blue (quarterly trend)
- 🔵 **30-day rolling average** - Light blue (monthly trend)
- 🔵 **7-day rolling average** - Lightest blue (weekly acute state)

**Right Y-Axis (Sleep hours):**
- 🟣 **Weekly average sleep** - Purple dashed line, 4px width (very prominent)

#### Chart 2: Resting Heart Rate Rolling Averages with Weekly Sleep
**Left Y-Axis (RHR in bpm):**
- 🔴 **365-day rolling average** - Dark red, thickest line (annual baseline)
- 🔴 **90-day rolling average** - Medium red (quarterly trend)
- 🟠 **30-day rolling average** - Orange (monthly trend)
- 🟡 **7-day rolling average** - Light orange (weekly acute state)

**Right Y-Axis (Sleep hours):**
- 🟣 **Weekly average sleep** - Purple dashed line, 4px width (same as HRV chart)

---

## 🎯 Key Features

### Data Processing
- **Full year calculation**: Fetches 365 days of metrics to calculate accurate rolling averages
- **Display window**: Shows last 90 days on charts (configurable in code)
- **Smart filtering**: Only displays charts if at least 7 days of data available
- **Pandas rolling**: Uses pandas `.rolling()` for efficient window calculations

### Chart Interactions
- **Unified hover**: Hover over any date to see all values at once (all rolling averages + sleep)
- **Responsive layout**: Charts stretch to full container width
- **Legend**: Horizontal layout above each chart for easy reference
- **Color coding**: Darker colors = longer timeframes, lighter = shorter timeframes

### Visual Design
- **Height**: 500px per chart (good for detail visibility)
- **Purple sleep line**: Highly visible 4px dashed line on secondary axis
- **Consistent sleep**: Same purple line on both charts for easy comparison
- **Professional colors**: Blues for HRV, reds/oranges for RHR (intuitive associations)

---

## 📦 Dependencies Added

Added to `requirements.txt`:
```
plotly>=5.18.0
pandas>=2.1.0
```

Imports added to `app/ui/dashboard_view.py`:
```python
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
```

---

## 🔍 How It Works

### 1. Data Fetching
```python
# Fetch 365 days of metrics for rolling calculations
chart_start = date.today() - timedelta(days=365)
chart_metrics = session.scalars(
    select(DailyMetric)
    .where(DailyMetric.athlete_id == athlete.id)
    .where(DailyMetric.date >= chart_start)
    .order_by(DailyMetric.date)
).all()
```

### 2. Rolling Average Calculations
```python
# Create pandas DataFrame
df = pd.DataFrame([
    {'date': m.date, 'hrv': m.hrv, 'rhr': m.rhr, 'sleep': m.sleep_hours}
    for m in chart_metrics
])

# Calculate rolling windows
df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()
df['hrv_30d'] = df['hrv'].rolling(window=30, min_periods=1).mean()
df['hrv_90d'] = df['hrv'].rolling(window=90, min_periods=1).mean()
df['hrv_365d'] = df['hrv'].rolling(window=365, min_periods=1).mean()

# Same for RHR and weekly sleep
df['sleep_weekly'] = df['sleep'].rolling(window=7, min_periods=1).mean()
```

### 3. Chart Rendering
```python
# Create dual-axis subplot
fig_hrv = make_subplots(specs=[[{"secondary_y": True}]])

# Add HRV lines to primary axis
fig_hrv.add_trace(
    go.Scatter(x=df_display['date'], y=df_display['hrv_365d'], 
              name='HRV 365-day', line=dict(color='#1f77b4', width=2.5)),
    secondary_y=False
)

# Add sleep line to secondary axis
fig_hrv.add_trace(
    go.Scatter(x=df_display['date'], y=df_display['sleep_weekly'], 
              name='Avg Sleep (weekly)', 
              line=dict(color='#9467bd', width=4, dash='dash')),
    secondary_y=True
)
```

---

## 🚀 Usage

### For Athletes
1. **Run historical sync** - Click "Sync Last 365 Days" in sidebar (one-time setup)
2. **View charts** - Scroll down dashboard to see "📈 Recovery Metrics Trends"
3. **Interpret trends**:
   - **Converging lines** (all rolling averages close together) = stable, consistent metrics
   - **Diverging lines** (wide gaps) = recent changes in recovery state
   - **7-day line above longer averages** = improving recovery (for HRV) or decreasing stress (for RHR)
   - **7-day line below longer averages** = declining recovery, potential overtraining
   - **Purple sleep line dipping** = insufficient sleep may be impacting recovery

### For Coaches
1. **Compare trends across athletes** - Look for athletes whose 7-day lines are diverging significantly
2. **Identify patterns** - Sleep drops often precede HRV drops by 1-2 days
3. **Early intervention** - 7-day line crossing below 30-day = warning sign
4. **Periodization feedback** - 90-day and 365-day lines show training block impacts

---

## 📊 Interpreting the Charts

### HRV Chart (Higher is Better)
| Pattern | Meaning | Action |
|---------|---------|--------|
| 7-day > 30-day > 90-day | Improving recovery trend | Green light for training progression |
| 7-day < 30-day < 90-day | Declining recovery | Consider rest or load reduction |
| Large gap 7-day vs 30-day | Recent acute change | Investigate: illness, stress, travel? |
| All lines flat and stable | Homeostasis | Consistent training load maintained |

### RHR Chart (Lower is Better)
| Pattern | Meaning | Action |
|---------|---------|--------|
| 7-day < 30-day < 90-day | Improving fitness | Positive adaptation to training |
| 7-day > 30-day > 90-day | Increasing stress | Potential overtraining, illness, or fatigue |
| Sudden 7-day spike | Acute stress response | Check for: poor sleep, illness, overreaching |
| Gradual 30-day/90-day decline | Fitness improvement | Long-term positive adaptation |

### Sleep Overlay (Purple Line)
- **Parallel to HRV** - Sleep supporting recovery
- **Drops before HRV drops** - Sleep deficit leading indicator
- **Consistently below 7 hours** - Chronic sleep debt impacting recovery
- **High variability** - Inconsistent sleep schedule affecting metrics

---

## 🎨 Customization Options

### Change Display Window
In `dashboard_view.py`, line ~95:
```python
display_days = 90  # Change to 30, 60, 180, etc.
df_display = df[df['date'] >= (chart_end - timedelta(days=display_days))].copy()
```

### Adjust Line Colors
HRV colors (blues):
```python
'#1f77b4'  # 365-day - dark blue
'#4a90e2'  # 90-day - medium blue
'#7ab8ff'  # 30-day - light blue
'#a8d5ff'  # 7-day - lightest blue
```

RHR colors (reds/oranges):
```python
'#8b0000'  # 365-day - dark red
'#d62728'  # 90-day - medium red
'#ff7f0e'  # 30-day - orange
'#ffbb78'  # 7-day - light orange
```

Sleep color (consistent):
```python
'#9467bd'  # Purple - stands out on both charts
```

### Change Line Thickness
```python
line=dict(color='#1f77b4', width=2.5)  # width: 1-5 reasonable range
```

### Modify Rolling Windows
```python
df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()  # Change 7 to 5, 10, 14, etc.
```

---

## 🧪 Testing Checklist

### ✅ Installation
- [x] `plotly>=5.18.0` installed
- [x] `pandas>=2.1.0` installed (was already present)
- [x] No import errors in dashboard

### ⬜ Data Requirements
- [ ] At least 7 days of metrics data in database
- [ ] HRV, RHR, and Sleep values populated (not all null)
- [ ] Historical sync completed (365 days recommended)

### ⬜ Visual Verification
- [ ] Charts appear between alerts section and manual sync button
- [ ] HRV chart shows 4 blue lines + 1 purple dashed line
- [ ] RHR chart shows 4 red/orange lines + 1 purple dashed line
- [ ] Hover shows all values for selected date
- [ ] Legend appears above each chart horizontally
- [ ] Charts responsive to window resizing

### ⬜ Data Accuracy
- [ ] Rolling averages make sense (7-day most volatile, 365-day smoothest)
- [ ] Sleep values displayed on right y-axis (typically 5-10 hours)
- [ ] HRV values displayed on left y-axis (typically 20-120ms)
- [ ] RHR values displayed on left y-axis (typically 35-80 bpm)

---

## 🐛 Troubleshooting

### "Need at least 7 days of metrics data" Message
**Cause**: Insufficient data in `daily_metrics` table
**Solution**: 
1. Click "Sync Last 365 Days" in sidebar
2. Wait for sync to complete
3. Refresh page

### Charts Not Appearing
**Cause**: Missing data in database or import errors
**Solution**:
```powershell
# Check for errors
python -c "import plotly, pandas; print('OK')"

# Verify data exists
psql <your_database> -c "SELECT COUNT(*) FROM daily_metrics WHERE athlete_id = 1;"
```

### Rolling Averages Look Wrong
**Cause**: Missing or null values in metrics creating gaps
**Solution**: Check for nulls:
```sql
SELECT 
    COUNT(*) as total_rows,
    COUNT(hrv) as hrv_count,
    COUNT(rhr) as rhr_count,
    COUNT(sleep_hours) as sleep_count
FROM daily_metrics
WHERE athlete_id = 1 AND date >= NOW() - INTERVAL '365 days';
```
If many nulls, pandas `.rolling()` with `min_periods=1` will still calculate but may show volatile lines early.

### Lines Appear Choppy or Jagged
**Cause**: Data gaps (days with no metrics logged)
**Solution**: Normal behavior. Pandas interpolates between points. Athletes should log metrics daily for smoothest lines.

### Secondary Y-Axis (Sleep) Not Visible
**Cause**: Sleep values much smaller than HRV/RHR, axis scaling issue
**Solution**: Already handled with separate `secondary_y=True/False` in code. If issue persists, manually set y-axis range:
```python
fig_hrv.update_yaxes(range=[5, 10], secondary_y=True)  # Force 5-10 hour range
```

---

## 🔮 Future Enhancements

### Possible Additions
1. **Shaded ranges** - Show normal baseline range (mean ± 1 std dev) as filled area
2. **Annotations** - Mark race days, illness, travel with vertical lines
3. **Training load overlay** - Add TSS or CTL as third y-axis
4. **Comparison mode** - Select two athletes to compare side-by-side
5. **Export functionality** - Download chart as PNG or PDF
6. **Date range selector** - Let user choose display window (30/60/90/180/365 days)
7. **Metric selector** - Toggle which rolling windows to display
8. **Zoom/pan tools** - Interactive Plotly controls for detailed examination

### Integration with Alerts
- **Highlight alert dates** - Color background when deviation alerts triggered
- **Threshold lines** - Show baseline ± 1σ as reference lines
- **Alert markers** - Add dots/icons on dates when alerts generated

---

## 📚 Related Documentation
- `BASELINE_SYSTEM_SUMMARY.md` - Complete baseline monitoring system overview
- `OAUTH_FIX_SUMMARY.md` - OAuth token exchange troubleshooting
- `METRICS_FIX.md` - Field name mapping corrections
- `project_history.md` - Full development timeline

---

## 📈 Chart Examples

### Well-Recovered Athlete
```
HRV Chart:
- All lines trending upward or stable
- 7-day line above or equal to 30-day
- Purple sleep line consistently 7-9 hours
- Minimal gaps between rolling averages

RHR Chart:
- All lines trending downward or stable  
- 7-day line below or equal to 30-day
- Corresponds with high HRV values
```

### Overreaching/Fatigued Athlete
```
HRV Chart:
- 7-day line dropping below 30-day and 90-day
- Widening gap between short and long-term averages
- Purple sleep line may be dropping too
- Diverging pattern

RHR Chart:
- 7-day line rising above 30-day and 90-day
- Corresponds with dropping HRV
- Sleep often insufficient (<7 hours)
```

### Training Block Adaptation
```
HRV Chart:
- Initial dip in 7-day and 30-day lines (training stress)
- 90-day and 365-day remain stable (underlying fitness)
- Recovery week: 7-day rebounds above previous baseline
- Supercompensation visible

RHR Chart:
- Initial spike in 7-day line during hard block
- Recovery period: 7-day drops below previous baseline
- Long-term downward trend (fitness improving)
```

---

**Date Implemented:** October 2, 2025
**Feature:** Dual-axis rolling average charts for HRV and RHR with sleep overlay
**Status:** ✅ Complete and ready for testing
**Next Step:** Run `streamlit run app/main.py` and verify charts appear with data
