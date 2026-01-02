# FIT File Analysis Examples

This document shows how to use the `fit_analysis.py` module to analyze FIT files downloaded from TrainingPeaks.

## Quick Start

### Using the Test Interface

The easiest way to analyze FIT files is through the test automation helper:

```bash
python test_automation_helper.py
# Select Option 11: Analyze FIT file
```

### Programmatic Usage

```python
from app.services.fit_analysis import FitFileAnalyzer, analyze_fit_file, get_fit_summary

# Quick summary (fastest - no time-series data)
summary = get_fit_summary("workout.fit")
print(summary)

# Full analysis with time-series data
analysis = analyze_fit_file("workout.fit", include_records=True)

# Access specific data
print(f"Sport: {analysis.session.sport}")
print(f"Duration: {analysis.session.total_timer_time} seconds")
print(f"Distance: {analysis.session.total_distance} meters")
print(f"Avg HR: {analysis.session.avg_heart_rate} bpm")
print(f"Avg Power: {analysis.session.avg_power} watts")
```

## Analysis Options

### 1. Quick Summary (Fastest)

Gets session and lap data without loading all time-series records:

```python
analyzer = FitFileAnalyzer("workout.fit")
summary = analyzer.get_quick_summary()

# Returns:
{
    'sport': 'running',
    'duration_seconds': 3600.0,
    'distance_km': 10.5,
    'distance_miles': 6.52,
    'avg_pace_per_km': '5:43',
    'avg_pace_per_mile': '9:12',
    'avg_heart_rate': 145,
    'max_heart_rate': 172,
    'avg_power': 250,
    'max_power': 450,
    'total_calories': 650,
    'lap_count': 5,
    'record_count': 3600
}
```

### 2. Full Analysis

Includes all time-series data points:

```python
analysis = analyzer.analyze(include_records=True)

# Access session data
print(f"Sport: {analysis.session.sport}")
print(f"TSS: {analysis.session.training_stress_score}")
print(f"IF: {analysis.session.intensity_factor}")
print(f"NP: {analysis.session.normalized_power}")

# Access laps
for lap in analysis.laps:
    print(f"Lap {lap.lap_number}:")
    print(f"  Time: {lap.total_timer_time}s")
    print(f"  Distance: {lap.total_distance}m")
    print(f"  Avg HR: {lap.avg_heart_rate}")
    print(f"  Avg Power: {lap.avg_power}W")

# Access time-series records
for record in analysis.records:
    print(f"{record.timestamp}: HR={record.heart_rate}, Power={record.power}")

# Access device info
for device in analysis.devices:
    print(f"{device.manufacturer} {device.product}")
```

### 3. Heart Rate Zone Analysis

Calculate time spent in HR zones:

```python
zones = analyzer.extract_hr_zones(max_hr=190)

# Returns:
{
    'Zone 1 (50-60%)': 5.2,   # minutes
    'Zone 2 (60-70%)': 18.7,
    'Zone 3 (70-80%)': 25.3,
    'Zone 4 (80-90%)': 8.1,
    'Zone 5 (90-100%)': 2.7,
    'total_minutes': 60.0
}
```

### 4. Power Curve Analysis

Find best average power for different durations:

```python
# Default durations (5s, 10s, 20s, 30s, 1min, 2min, 5min, 10min, 20min, 30min, 60min)
power_curve = analyzer.extract_power_curve()

# Custom durations
power_curve = analyzer.extract_power_curve(durations=[5, 60, 300, 1200])

# Returns:
{
    5: 850,      # 5-second max avg power: 850W
    60: 420,     # 1-minute max avg power: 420W
    300: 310,    # 5-minute max avg power: 310W
    1200: 280    # 20-minute max avg power: 280W
}
```

### 5. Export to JSON

```python
analysis = analyzer.analyze(include_records=True)
data = analysis.to_dict()

import json
with open('workout_analysis.json', 'w') as f:
    json.dump(data, f, indent=2, default=str)
```

## Available Metrics

### Session Metrics
- `sport` - Sport type (running, cycling, swimming, etc.)
- `sub_sport` - Sub-sport type
- `start_time` - Workout start timestamp
- `total_elapsed_time` - Total elapsed time (seconds)
- `total_timer_time` - Active/moving time (seconds)
- `total_distance` - Distance (meters)
- `total_calories` - Total calories burned
- `avg_speed` / `max_speed` - Speed (m/s)
- `avg_heart_rate` / `max_heart_rate` - Heart rate (bpm)
- `avg_power` / `max_power` - Power (watts)
- `normalized_power` - Normalized Power (NP)
- `avg_cadence` / `max_cadence` - Cadence (rpm or spm)
- `total_ascent` / `total_descent` - Elevation gain/loss (meters)
- `training_stress_score` - TSS
- `intensity_factor` - IF

### Lap Metrics
- `lap_number` - Lap number (1-indexed)
- `start_time` - Lap start timestamp
- `total_elapsed_time` - Lap elapsed time (seconds)
- `total_timer_time` - Lap active time (seconds)
- `total_distance` - Lap distance (meters)
- `avg_speed` / `max_speed` - Lap speed (m/s)
- `avg_heart_rate` / `max_heart_rate` - Lap HR (bpm)
- `avg_power` / `max_power` - Lap power (watts)
- `avg_cadence` / `max_cadence` - Lap cadence
- `total_calories` - Lap calories
- `avg_temperature` - Lap temperature (celsius)

### Record (Time-Series) Metrics
- `timestamp` - Data point timestamp
- `position_lat` / `position_long` - GPS coordinates (semicircles)
- `distance` - Cumulative distance (meters)
- `altitude` - Elevation (meters)
- `speed` - Speed at this point (m/s)
- `heart_rate` - Heart rate (bpm)
- `cadence` - Cadence (rpm/spm)
- `power` - Power (watts)
- `temperature` - Temperature (celsius)

### Device Info
- `manufacturer` - Device manufacturer
- `product` - Device model
- `serial_number` - Device serial number
- `software_version` - Firmware version
- `device_index` - Device index in FIT file

## Integration with TrainingPeaks Downloads

```python
from app.services.tp_api import TrainingPeaksAPI
from app.services.fit_analysis import analyze_fit_file

# Download FIT file from TrainingPeaks
api = TrainingPeaksAPI(athlete_id=123)
result = api.fetch_workout_file(
    workout_id="3444886827",
    file_format="fit",
    tp_athlete_id=5302165
)

# Save to file
file_path = f"workout_{result['workout_id']}.fit"
with open(file_path, 'wb') as f:
    f.write(result['content'])

# Analyze the downloaded file
analysis = analyze_fit_file(file_path, include_records=True)

# Display summary
summary = analysis.get_summary()
print(f"Sport: {summary['sport']}")
print(f"Distance: {summary['distance_km']} km")
print(f"Duration: {summary['duration_seconds']} seconds")
print(f"Avg HR: {summary['avg_heart_rate']} bpm")
```

## Performance Considerations

- **Quick Summary**: Fast, only loads session and lap data
- **Full Analysis**: Slower, loads all time-series records (can be 1000s of points)
- **HR Zones**: Requires full analysis with records
- **Power Curve**: Requires full analysis with records, can be slow for long workouts

For large files (>2 hours of data), consider:
1. Use quick summary first to check file size
2. Only do full analysis when needed
3. Don't export to JSON unless necessary (records can be very large)

## Error Handling

```python
from pathlib import Path

try:
    analyzer = FitFileAnalyzer("workout.fit")
    analysis = analyzer.analyze()
except FileNotFoundError:
    print("FIT file not found")
except Exception as e:
    print(f"Error analyzing FIT file: {e}")
```

## Future Enhancements

Potential additions to the analysis module:
-VO2 max estimation
- Training load calculation
- Pace/power zone analysis
- Anomaly detection (data spikes)
- GPS route visualization
- Comparison between workouts
- Auto-detection of intervals
