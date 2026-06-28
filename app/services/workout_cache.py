"""
Workout Timeseries Cache Service

File-based caching for TrainingPeaks workout timeseries data.
Avoids repeated expensive API calls (120s+ per request) by storing
the full JSON payload on disk and compact summaries in the database.

Cache location: data/timeseries_cache/ts_{tp_workout_id}.json
FIT file location: workout_files/{tp_workout_id}.fit

Why files instead of Postgres:
  - Timeseries payloads are 1-5 MB each (34K+ lines of JSON)
  - Storing in a JSON column would balloon the DB and slow queries
  - File cache is trivial to prune, back up, or regenerate
"""

import json
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

from app.utils.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

# ── Cache directories ────────────────────────────────────────────────────────

TIMESERIES_CACHE_DIR = PROJECT_ROOT / "data" / "timeseries_cache"
FIT_CACHE_DIR = PROJECT_ROOT / "workout_files"


def _ensure_dirs():
    """Create cache directories if they don't exist."""
    TIMESERIES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Timeseries JSON cache ────────────────────────────────────────────────────

def _ts_cache_path(tp_workout_id: str | int) -> Path:
    """Return the canonical cache file path for a given workout ID."""
    return TIMESERIES_CACHE_DIR / f"ts_{tp_workout_id}.json"


def get_cached_timeseries(tp_workout_id: str | int) -> Optional[dict]:
    """Load cached timeseries JSON from disk, or return None on miss.

    Returns the full payload dict (WorkoutChannels, WorkoutStats, LapStats, etc.).
    """
    path = _ts_cache_path(tp_workout_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Cache HIT for timeseries %s (%s)", tp_workout_id, path.name)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cache read error for %s: %s", tp_workout_id, e)
        return None


def save_timeseries(tp_workout_id: str | int, payload: dict) -> Path:
    """Write timeseries JSON payload to disk cache.

    Returns the path of the saved file.
    """
    _ensure_dirs()
    path = _ts_cache_path(tp_workout_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    size_kb = path.stat().st_size / 1024
    logger.info("Cached timeseries %s → %s (%.0f KB)", tp_workout_id, path.name, size_kb)
    return path


def delete_cached_timeseries(tp_workout_id: str | int) -> bool:
    """Remove a cached timeseries file. Returns True if deleted."""
    path = _ts_cache_path(tp_workout_id)
    if path.exists():
        path.unlink()
        return True
    return False


def is_timeseries_cached(tp_workout_id: str | int) -> bool:
    """Check if a timeseries cache file exists without loading it."""
    return _ts_cache_path(tp_workout_id).exists()


# ── FIT file cache ────────────────────────────────────────────────────────────

def _fit_cache_path(tp_workout_id: str | int) -> Path:
    return FIT_CACHE_DIR / f"{tp_workout_id}.fit"


def get_cached_fit(tp_workout_id: str | int) -> Optional[bytes]:
    """Load cached FIT file bytes, or None on miss."""
    path = _fit_cache_path(tp_workout_id)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        logger.info("Cache HIT for FIT %s (%s, %.0f KB)", tp_workout_id, path.name, len(data) / 1024)
        return data
    except OSError as e:
        logger.warning("FIT cache read error for %s: %s", tp_workout_id, e)
        return None


def save_fit(tp_workout_id: str | int, content: bytes) -> Path:
    """Write FIT file bytes to disk cache."""
    _ensure_dirs()
    path = _fit_cache_path(tp_workout_id)
    path.write_bytes(content)
    logger.info("Cached FIT %s → %s (%.0f KB)", tp_workout_id, path.name, len(content) / 1024)
    return path


def is_fit_cached(tp_workout_id: str | int) -> bool:
    return _fit_cache_path(tp_workout_id).exists()


# ── FIT file parsing ─────────────────────────────────────────────────────────

# Map FIT record field names to the TP-style channel names used downstream.
_FIT_FIELD_TO_CHANNEL = [
    ("speed", "Speed"),
    ("enhanced_speed", "Speed"),
    ("heart_rate", "HeartRate"),
    ("power", "Power"),
    ("cadence", "Cadence"),
    ("enhanced_altitude", "Elevation"),
    ("altitude", "Elevation"),
    ("distance", "Distance"),
    ("vertical_oscillation", "VerticalOscillation"),
    ("stance_time", "StanceTime"),
    ("step_length", "StepLength"),
]


def parse_fit_to_timeseries(fit_bytes: bytes) -> dict:
    """Parse a FIT activity file into the same dict format as the TP details API.

    Returns a dict with the same shape as fetch_workout_time_series():
        {
            "WorkoutChannels": {
                "Channels": ["Speed", "HeartRate", "Power", ...],
                "Data": [
                    {"Event": "Start", "MillisecondOffset": 0, "Values": [...]},
                    ...
                ]
            }
        }
    """
    import io
    import fitparse

    fit_file = fitparse.FitFile(io.BytesIO(fit_bytes))
    records = list(fit_file.get_messages("record"))
    if not records:
        return {"WorkoutChannels": {"Channels": [], "Data": []}}

    # Discover which channels are present by scanning ALL records.
    # Scanning only records[0] is unreliable: power, cadence, and heart_rate
    # commonly appear several seconds into a ride (sensor pairing / pre-pedalling),
    # so the first record may be missing them even though they're present throughout
    # the rest of the file. Bug repro: a FIT with power from second 8 onward would
    # have its entire power channel dropped under the old logic.
    all_field_names: set[str] = set()
    for rec in records:
        for f in rec.fields:
            all_field_names.add(f.name)

    channels: list[str] = []
    field_keys: list[str] = []  # parallel list of FIT field names
    seen_channels: set[str] = set()
    for fit_name, ch_name in _FIT_FIELD_TO_CHANNEL:
        if fit_name in all_field_names and ch_name not in seen_channels:
            channels.append(ch_name)
            field_keys.append(fit_name)
            seen_channels.add(ch_name)

    # Determine the base timestamp from the first record.
    base_ts = None
    for f in records[0].fields:
        if f.name == "timestamp" and f.value is not None:
            base_ts = f.value
            break

    data: list[dict] = []
    for i, rec in enumerate(records):
        # Build a quick lookup of field values for this record.
        field_vals = {f.name: f.value for f in rec.fields}

        # Compute millisecond offset
        ts = field_vals.get("timestamp")
        if base_ts is not None and ts is not None:
            delta = ts - base_ts
            ms_offset = int(delta.total_seconds() * 1000)
        else:
            ms_offset = i * 1000  # fallback: 1-second spacing

        values = []
        for fk in field_keys:
            v = field_vals.get(fk)
            values.append(float(v) if v is not None else 0.0)

        event = "Start" if i == 0 else ("Stop" if i == len(records) - 1 else "")
        data.append({
            "Event": event,
            "MillisecondOffset": ms_offset,
            "Values": values,
        })

    return {"WorkoutChannels": {"Channels": channels, "Data": data}}


# ── Cache-through API fetch ──────────────────────────────────────────────────

def fetch_timeseries_cached(api, tp_workout_id: str, tp_athlete_id: int) -> dict:
    """Fetch timeseries with cache-through: return cached if available, else fetch & cache.

    Primary path: download the FIT file (fast, small binary) and parse locally.
    Fallback: use the TP details/timeseries JSON endpoint if no FIT is available.

    Args:
        api: TrainingPeaksAPI instance (already authenticated)
        tp_workout_id: The TP workout ID
        tp_athlete_id: The TP athlete ID for scoped access

    Returns:
        The full timeseries payload dict
    """
    # 1. Check JSON cache first (covers previously fetched data via either path)
    cached = get_cached_timeseries(tp_workout_id)
    if cached is not None:
        return cached

    # 2. Try FIT file: download, parse, cache the resulting JSON
    logger.info("Cache MISS for timeseries %s — trying FIT download...", tp_workout_id)
    try:
        fit_bytes = fetch_fit_cached(api, tp_workout_id, tp_athlete_id)
        payload = parse_fit_to_timeseries(fit_bytes)
        # Only cache if we actually got data rows
        if (payload.get("WorkoutChannels") or {}).get("Data"):
            save_timeseries(tp_workout_id, payload)
            return payload
        logger.warning("FIT file for %s parsed but contained no record data.", tp_workout_id)
    except Exception as e:
        logger.warning("FIT download/parse failed for %s: %s — falling back to details API.", tp_workout_id, e)

    # 3. Fallback: TP details JSON endpoint (slow, may timeout for large files)
    logger.info("Falling back to details API for %s...", tp_workout_id)
    payload = api.fetch_workout_time_series(tp_workout_id, tp_athlete_id=tp_athlete_id)
    save_timeseries(tp_workout_id, payload)
    return payload


def fetch_fit_cached(api, tp_workout_id: str, tp_athlete_id: int) -> bytes:
    """Fetch FIT file with cache-through: return cached if available, else fetch & cache."""
    cached = get_cached_fit(tp_workout_id)
    if cached is not None:
        return cached

    logger.info("Cache MISS for FIT %s — fetching from TP API...", tp_workout_id)
    result = api.fetch_workout_file(tp_workout_id, file_format="fit", tp_athlete_id=tp_athlete_id)
    content = result["content"]
    if isinstance(content, bytes):
        save_fit(tp_workout_id, content)
        return content
    raise RuntimeError(f"Unexpected FIT content type: {type(content)}")


# ── Summary extraction ────────────────────────────────────────────────────────

def extract_workout_summary(payload: dict) -> dict:
    """Extract the compact WorkoutStats dict from a timeseries payload.

    Returns a flat dict suitable for DB storage or quick display.
    """
    stats = payload.get("WorkoutStats") or {}
    return {
        "name": stats.get("Name"),
        "elapsed_time_ms": stats.get("ElapsedTimeMs"),
        "tss": stats.get("Tss"),
        "intensity_factor": stats.get("IF"),
        "normalized_power": stats.get("NormalizedPower"),
        "power_average": stats.get("PowerAverage"),
        "power_maximum": stats.get("PowerMaximum"),
        "hr_average": stats.get("HeartRateAverage"),
        "hr_maximum": stats.get("HeartRateMaximum"),
        "hr_minimum": stats.get("HeartRateMinimum"),
        "speed_average": stats.get("SpeedAverage"),
        "speed_maximum": stats.get("SpeedMaximum"),
        "normalized_speed": stats.get("NormalizedSpeed"),
        "cadence_average": stats.get("CadenceAverage"),
        "cadence_maximum": stats.get("CadenceMaximum"),
        "energy_kj": stats.get("Energy"),
        "elevation_gain": stats.get("ElevationGain"),
        "elevation_loss": stats.get("ElevationLoss"),
        "watts_per_kg": stats.get("WattsPerKg"),
        "efficiency_factor": stats.get("EfficiencyFactor"),
        "power_pulse_decoupling": stats.get("PowerPulseDecoupling"),
        "speed_pulse_decoupling": stats.get("SpeedPulseDecoupling"),
        "vi": stats.get("VI"),
    }


def extract_lap_summaries(payload: dict) -> list[dict]:
    """Extract LapStats from a timeseries payload as a list of dicts.

    Each dict contains: lap_number, name, duration_ms, avg_power, avg_hr, avg_speed, etc.
    """
    laps_raw = payload.get("LapStats") or []
    laps = []
    for i, lap in enumerate(laps_raw, start=1):
        laps.append({
            "lap_number": i,
            "name": lap.get("Name"),
            "start_time_ms": lap.get("StartTimeMs"),
            "end_time_ms": lap.get("EndTimeMs"),
            "elapsed_time_ms": lap.get("ElapsedTimeMs"),
            "tss": lap.get("Tss"),
            "intensity_factor": lap.get("IF"),
            "normalized_power": lap.get("NormalizedPower"),
            "power_average": lap.get("PowerAverage"),
            "power_maximum": lap.get("PowerMaximum"),
            "hr_average": lap.get("HeartRateAverage"),
            "hr_maximum": lap.get("HeartRateMaximum"),
            "hr_minimum": lap.get("HeartRateMinimum"),
            "speed_average": lap.get("SpeedAverage"),
            "speed_maximum": lap.get("SpeedMaximum"),
            "cadence_average": lap.get("CadenceAverage"),
            "energy_kj": lap.get("Energy"),
            "elevation_gain": lap.get("ElevationGain"),
            "watts_per_kg": lap.get("WattsPerKg"),
        })
    return laps


# ── Timeseries data normalization ─────────────────────────────────────────────

def normalize_timeseries_rows(payload: dict) -> tuple[list[str], list[list]]:
    """Convert TP timeseries payload into a uniform (channels, rows) format.

    The TP API returns Data as a list of dicts:
        {"Event": "...", "MillisecondOffset": 1000, "Values": [148.0, 0.0, ...]}

    This function normalizes that into:
        channels = ["MillisecondOffset", "HeartRate", "Speed", ...]
        rows = [[0, 148.0, 0.0, ...], [1000, 138.0, 0.0, ...], ...]

    Also handles older flat-list format for backward compatibility.
    """
    wc = payload.get("WorkoutChannels") or {}
    channels_raw = wc.get("Channels") or []
    data = wc.get("Data") or []

    # Normalize channel names
    channels: list[str] = []
    for c in channels_raw:
        if isinstance(c, str):
            channels.append(c)
        elif isinstance(c, dict):
            name = c.get("Name") or c.get("name") or c.get("Channel") or c.get("channel")
            channels.append(str(name or ""))
        else:
            channels.append(str(c))

    if not data:
        return channels, []

    # Detect format: dict rows (standard TP API) vs flat list rows (legacy)
    first = data[0]
    if isinstance(first, dict):
        # Standard TP format: {Event, MillisecondOffset, Values}
        out_channels = ["MillisecondOffset"] + channels
        rows = []
        for entry in data:
            offset = entry.get("MillisecondOffset", 0)
            values = entry.get("Values") or []
            rows.append([offset] + list(values))
        return out_channels, rows
    elif isinstance(first, list):
        # Legacy flat-list format
        return channels, [list(r) for r in data]
    else:
        return channels, []


# ── List cached workouts ──────────────────────────────────────────────────────

def list_cached_timeseries() -> list[str]:
    """Return list of cached workout IDs."""
    if not TIMESERIES_CACHE_DIR.exists():
        return []
    return [
        p.stem.replace("ts_", "")
        for p in TIMESERIES_CACHE_DIR.glob("ts_*.json")
    ]


def list_cached_fits() -> list[str]:
    """Return list of cached FIT workout IDs."""
    if not FIT_CACHE_DIR.exists():
        return []
    return [p.stem for p in FIT_CACHE_DIR.glob("*.fit")]


def cache_stats() -> dict:
    """Return cache statistics."""
    ts_files = list(TIMESERIES_CACHE_DIR.glob("ts_*.json")) if TIMESERIES_CACHE_DIR.exists() else []
    fit_files = list(FIT_CACHE_DIR.glob("*.fit")) if FIT_CACHE_DIR.exists() else []
    ts_size = sum(f.stat().st_size for f in ts_files)
    fit_size = sum(f.stat().st_size for f in fit_files)
    return {
        "timeseries_count": len(ts_files),
        "timeseries_size_mb": round(ts_size / (1024 * 1024), 1),
        "fit_count": len(fit_files),
        "fit_size_mb": round(fit_size / (1024 * 1024), 1),
        "total_size_mb": round((ts_size + fit_size) / (1024 * 1024), 1),
    }
