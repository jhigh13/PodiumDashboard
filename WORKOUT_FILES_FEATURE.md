# Workout File Download Feature

## Overview
This feature allows you to download structured workout files from TrainingPeaks in various formats (FIT, JSON, MRC, ERG, ZWO) for detailed workout analysis.

## Implementation

### API Method: `fetch_workout_file()`
Added to `app/services/tp_api.py` in the `TrainingPeaksAPI` class.

**Signature:**
```python
def fetch_workout_file(self, workout_id: str, file_format: str = 'fit', tp_athlete_id: int | None = None)
```

**Parameters:**
- `workout_id`: The TrainingPeaks workout ID
- `file_format`: File format to download (fit, erg, mrc, zwo, json). Default is 'fit'
- `tp_athlete_id`: Optional athlete ID for athlete-scoped endpoint

**Returns:**
Dictionary with:
- `content`: bytes of the file (for binary formats) or dict (for json)
- `filename`: suggested filename from Content-Disposition header
- `format`: the requested format
- `workout_id`: the workout ID

**Supported Formats:**
- **FIT**: Binary format for Garmin devices and most training software
- **JSON**: Structured workout data with full detail
- **MRC**: Computrainer format
- **ERG**: Ergometer format
- **ZWO**: Zwift workout format

### Test Interface: Option 9
Added to `test_automation_helper.py` as menu option 9.

**Features:**
1. Select an athlete from your database
2. Fetches workouts for the effective "today" (accounting for sandbox offset)
3. Choose file format (FIT, JSON, MRC, ERG, ZWO)
4. Downloads all workouts for that day
5. Saves files to `workout_files/` directory
6. Provides detailed summary of successful/failed downloads

**Usage:**
```bash
python test_automation_helper.py
# Select option 9: Download FIT files for single athlete (single day)
```

## API Details

### TrainingPeaks Endpoint
- **URL**: `/v2/workouts/wod/file/{workout_id}/?format={format}`
- **Method**: GET
- **OAuth Scope Required**: `workouts:wod`
- **Documentation**: [TrainingPeaks Partners API - Workout File](https://github.com/TrainingPeaks/PartnersAPI/wiki/Workout-Of-The-Day-Structured-Workout-File)

### Important Notes
1. **Structured Workouts Only**: Only workouts with structure can be exported. Unstructured workouts will return a 400 error with message "Workout has no structure"
2. **Format Validation**: The API validates that the workout can be exported to the requested format
3. **File Download**: Binary formats (FIT, MRC, ERG, ZWO) are returned as bytes, JSON is parsed
4. **Filename Extraction**: Filename is extracted from `Content-Disposition` header

## OAuth Scope Requirement

⚠️ **IMPORTANT**: You need the `workouts:wod` scope for this feature to work.

When logging in via Option 1, make sure to select:
- **Coach** role (which includes `workouts:wod` scope)
- OR manually add `workouts:wod` to the scope list in the OAuth flow

## Use Cases

### 1. Deep Workout Analysis
Download FIT files to analyze:
- Heart rate zones
- Power data
- Cadence
- GPS/route data
- Interval structure

### 2. Third-Party Integration
Export workouts to:
- Training analysis software (TrainingPeaks, WKO, Golden Cheetah)
- Indoor training platforms (Zwift, TrainerRoad)
- Custom analysis tools

### 3. Workout Structure Analysis
Use JSON format to:
- Analyze workout structure programmatically
- Extract interval details
- Build custom compliance checks
- Generate workout summaries

## Error Handling

The feature handles several error cases:
- **No structure**: Workout cannot be exported (returns clear error message)
- **Invalid format**: Format not supported for this workout
- **Workout not found**: 404 error
- **No workout ID**: Skips workout with warning

## Example Output

```
====================================================================
 DOWNLOAD FIT FILES
====================================================================

📅 Date Information:
  Actual today: 2024-01-15
  Sandbox offset: 180 days
  Effective 'today': 2024-07-14
  Will download workouts for: 2024-07-14

👥 Available athletes:
  1. John Doe (TP ID: 12345)
  2. Jane Smith (TP ID: 67890)

Enter athlete ID: 1

✓ Selected: John Doe

====================================================================
FILE FORMAT OPTIONS:
====================================================================
  1. FIT (binary format for Garmin/devices)
  2. JSON (structured workout data)
  3. MRC (Computrainer format)
  4. ERG (Ergometer format)
  5. ZWO (Zwift format)

Select format (1-5) [default: 1 for FIT]: 1

✓ Selected format: FIT

📁 Output directory: C:\Users\johnk\VSCode\PodiumDashboard\workout_files

====================================================================
DOWNLOADING FILES:
====================================================================

[1/2] Downloading workout 123456789 (Bike)...
  ✓ Saved: C:\Users\johnk\VSCode\PodiumDashboard\workout_files\workout_123456789.fit
  ✓ Size: 45,678 bytes

[2/2] Downloading workout 987654321 (Run)...
  ✓ Saved: C:\Users\johnk\VSCode\PodiumDashboard\workout_files\workout_987654321.fit
  ✓ Size: 32,145 bytes

====================================================================
DOWNLOAD SUMMARY
====================================================================

✓ Successful: 2
❌ Failed: 0

📁 Downloaded files:
  - workout_123456789.fit (45,678 bytes) - Bike
  - workout_987654321.fit (32,145 bytes) - Run
```

## Next Steps

Potential enhancements:
1. **Batch Download**: Download multiple days/athletes at once
2. **FIT File Parsing**: Parse FIT files to extract detailed metrics
3. **Workout Analysis**: Analyze intervals, zones, and compliance
4. **Database Storage**: Store workout file metadata in database
5. **File Management**: Add UI to browse/delete downloaded files
6. **Auto-Download**: Automatically download files during daily ingestion
