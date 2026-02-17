# PodiumDashboard - File Memory

## File Changes (February 2026)

### Workout Timeseries Cache System (2026-02-14)

#### `app/services/workout_cache.py` (NEW)
- **Purpose**: File-based caching for TP workout timeseries data + FIT files
- **Key Functions**:
  - `fetch_timeseries_cached(api, wid, tp_aid)`: Cache-through fetch — disk cache first, API fallback, auto-save
  - `fetch_fit_cached(api, wid, tp_aid)`: Same pattern for FIT binary files
  - `normalize_timeseries_rows(payload)`: Converts TP dict-format Data into flat (channels, rows) tuples
  - `extract_workout_summary(payload)`: Extracts WorkoutStats into flat dict for DB storage
  - `extract_lap_summaries(payload)`: Extracts LapStats list for DB storage
  - `cache_stats()`: Returns count/size of cached files
- **Cache Locations**: `data/timeseries_cache/ts_{workout_id}.json`, `workout_files/{workout_id}.fit`
- **Why files not DB**: Timeseries payloads are 1-5 MB each; files are cheaper, easier to prune

#### `app/models/tables.py` — WorkoutDetail, WorkoutLap (NEW tables)
- **WorkoutDetail**: Compact summary stats per workout (NP, IF, TSS, HR, power, etc.) + cache timestamps
- **WorkoutLap**: Per-lap stats (duration, power, HR, speed, etc.)
- **Populated**: Automatically when timeseries is fetched and cached via race trace endpoint

#### `app/webapp/app.py` — race trace endpoint updates
- **Cache integration**: `/partials/race_tp_traces` now uses cache-through pattern (instant on repeat loads)
- **Auto-cache**: `/partials/race_tp_compare` pre-fetches default workouts in background threads
- **Data format fix**: `_extract_channels_payload` now handles TP dict-format rows (`{MillisecondOffset, Values}`)
- **Time handling**: Detects MillisecondOffset vs seconds and converts correctly

#### `app/data/db.py` — schema migration update
- `ensure_schema()` now auto-creates `workout_details` and `workout_laps` tables on startup
