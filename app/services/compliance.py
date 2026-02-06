"""Workout compliance evaluation service."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import get_session
from app.models.tables import Workout, WorkoutCompliance

DISTANCE_GOOD_PCT = 0.10
DISTANCE_OK_PCT = 0.20
DURATION_GOOD_PCT = 0.10
DURATION_OK_PCT = 0.20
SWIM_PACE_THRESHOLDS = (5.0, 8.0, 10.0)  # seconds per 100 (good, ok, bad)
RUN_PACE_THRESHOLDS = (10.0, 20.0, 30.0)  # seconds per mile (heuristic)
BIKE_POWER_THRESHOLDS = (10.0, 20.0, 25.0)  # watts (good, ok, bad)
RATING_TO_SCORE = {"good": 100, "ok": 70, "bad": 40}
METERS_PER_MILE = 1609.34
METERS_PER_YARD = 0.9144


def _extract_workout_title(raw_workout: Any) -> Optional[str]:
    if not isinstance(raw_workout, dict):
        return None
    title = (
        raw_workout.get("Title")
        or raw_workout.get("title")
        or raw_workout.get("WorkoutName")
        or raw_workout.get("workoutName")
        or raw_workout.get("Name")
        or raw_workout.get("name")
    )
    if title is None:
        return None
    s = str(title).strip()
    return s or None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # noqa: PERF203 - explicit for clarity
        return None


def _first_value(keys: Iterable[str], *sources: Optional[Dict[str, Any]]) -> Optional[float]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                num = _as_float(source.get(key))
                if num is not None:
                    return num
    return None


def _normalize_duration(raw: Optional[float]) -> Optional[float]:
    if raw is None:
        return None
    if raw <= 0:
        return None
    if raw < 20:  # Treat small numbers as hours (TrainingPeaks convention)
        return raw * 3600
    return raw


def _is_close_to_multiple(value: float, base: float, tolerance: float = 0.5) -> bool:
    if value is None or base <= 0:
        return False
    return abs(value - round(value / base) * base) <= tolerance


def _infer_swim_distance_unit_from_meters(distance_m: Optional[float]) -> str:
    """Infer whether a swim distance *authored in yards* was converted to meters.

    TrainingPeaks swim distances appear to be expressed in meters in the API, even when the
    workout was authored in yards (common in US pools). Yard-authored distances often appear as
    exact conversions to meters (yards * 0.9144).

    We treat the raw value as meters, and only infer yards when converting back to yards yields
    a clean, lane-like yard total (multiple of 25) within a small tolerance.
    """
    if distance_m is None or distance_m <= 0:
        return "m"
    yards = distance_m / METERS_PER_YARD
    yards_rounded = int(round(yards))
    if abs(yards - yards_rounded) <= 0.25 and yards_rounded % 25 == 0:
        return "yd"
    return "m"


def _normalize_distance_by_sport(
    sport: str,
    value: Optional[float],
    preferred_swim_unit: Optional[str] = None,
) -> Tuple[Optional[float], Optional[str]]:
    sport_lc = (sport or "").lower()
    if value is None:
        if sport_lc == "swim":
            default_unit = (preferred_swim_unit or "yd")
        else:
            default_unit = "mi" if sport_lc in {"run", "bike"} else None
        return None, default_unit

    if sport_lc == "swim":
        swim_unit = (preferred_swim_unit or _infer_swim_distance_unit_from_meters(value)).lower()
        if swim_unit == "yd":
            return float(value) / METERS_PER_YARD, "yd"
        return float(value), "m"
    if sport_lc in {"run", "bike"}:
        if value > 50:
            return float(value) / METERS_PER_MILE, "mi"
        return float(value), "mi"
    return float(value), None


def _seconds_to_minutes(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value / 60.0, 1)


def _seconds_to_time_string(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    total_seconds = int(round(value))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _percent_to_display(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{value * 100:.1f}%"


def _round_distance(value: Optional[float], sport: str) -> Optional[float]:
    if value is None:
        return None
    if sport == "swim":
        return int(round(value))
    if sport in {"run", "bike"}:
        return round(value, 2)
    return round(value, 2)


def _percent_delta(planned: float, actual: float) -> Optional[float]:
    if planned is None or actual is None or planned == 0:
        return None
    return abs(actual - planned) / planned


def _rate_percent(diff: Optional[float], good: float, ok: float) -> Optional[str]:
    if diff is None:
        return None
    if diff <= good:
        return "good"
    if diff <= ok:
        return "ok"
    return "bad"


def _rate_abs(diff: Optional[float], thresholds: Tuple[float, float, float]) -> Optional[str]:
    if diff is None:
        return None
    good, ok, bad = thresholds
    if diff <= good:
        return "good"
    if diff <= ok:
        return "ok"
    if diff <= bad:
        return "bad"
    return "bad"


def _collect_plan_summary(sport: str, raw_workout: Dict[str, Any]) -> Dict[str, Any]:
    """Extract planned workout values from TrainingPeaks API response.
    
    Uses TrainingPeaks *Planned fields (TotalTimePlanned, DistancePlanned, etc.)
    No fallback to actual values - keeps planned and actual completely separate.
    """
    # Extract planned values directly from TrainingPeaks API fields
    planned_duration = _normalize_duration(raw_workout.get('TotalTimePlanned'))
    planned_distance = _as_float(raw_workout.get('DistancePlanned'))  # meters
    preferred_swim_unit = None
    if (sport or "").lower() == "swim":
        preferred_swim_unit = _infer_swim_distance_unit_from_meters(planned_distance)
    normalized_distance, distance_unit = _normalize_distance_by_sport(
        sport,
        planned_distance,
        preferred_swim_unit=preferred_swim_unit,
    )
    
    # Planned pace/speed/power - typically not in workout list API, but check anyway
    planned_power = _as_float(raw_workout.get('PowerPlanned'))
    planned_velocity = _as_float(raw_workout.get('VelocityPlanned'))  # m/s
    
    sport_lc = (sport or "").lower()
    planned_speed_mph = None
    planned_pace_swim = None
    planned_pace_run = None
    
    # Calculate pace/speed from planned distance and duration
    if planned_duration and normalized_distance:
        if sport_lc == "swim":
            planned_pace_swim = planned_duration / (normalized_distance / 100)
        elif sport_lc == "run":
            planned_pace_run = planned_duration / max(normalized_distance, 1e-6)
            planned_speed_mph = normalized_distance / (planned_duration / 3600)
        elif sport_lc == "bike":
            planned_speed_mph = normalized_distance / (planned_duration / 3600)
    
    # If VelocityPlanned exists, convert from m/s to mph
    if planned_velocity and not planned_speed_mph:
        planned_speed_mph = planned_velocity * 2.23694  # m/s to mph

    return {
        "duration_seconds": planned_duration,
        "distance_value": normalized_distance,
        "distance_unit": distance_unit,
        "swim_pace_sec_per_100": planned_pace_swim,
        "run_pace_sec_per_mile": planned_pace_run,
        "average_speed_mph": planned_speed_mph,
        "power_watts": planned_power,
    }


def _duration_from_workout(workout: Workout) -> Optional[float]:
    if workout.duration_sec:
        return float(workout.duration_sec)
    raw = workout.raw_json or {}
    return _normalize_duration(
        _first_value(
            (
                "TotalTimeSeconds",
                "TotalTime",
                "Duration",
                "WorkoutDurationSeconds",
            ),
            raw,
        )
    )


def _collect_actual_summary(workout: Workout, preferred_swim_unit: Optional[str] = None) -> Dict[str, Any]:
    """Extract actual workout values from TrainingPeaks API response.
    
    Only returns actual values if Completed=true, otherwise returns None values.
    Uses TrainingPeaks actual fields (Distance, TotalTime, etc. - no Planned suffix).
    """
    raw = workout.raw_json or {}
    
    # Check if workout was actually completed
    completed = raw.get('Completed', False)
    
    if not completed:
        # Workout was planned but not completed - no actual values
        return {
            "duration_seconds": None,
            "distance_value": None,
            "distance_unit": None,
            "average_speed_mph": None,
            "swim_pace_sec_per_100": None,
            "run_pace_sec_per_mile": None,
            "power_watts": None,
            "completed": False,
        }
    
    # Workout was completed - extract actual values
    duration_seconds = _normalize_duration(raw.get('TotalTime'))  # decimal hours -> seconds
    distance = _as_float(raw.get('Distance'))  # meters, actual only
    normalized_distance, distance_unit = _normalize_distance_by_sport(
        workout.sport or "",
        distance,
        preferred_swim_unit=preferred_swim_unit,
    )
    
    avg_speed = _as_float(raw.get('VelocityAverage'))  # m/s
    avg_power = _as_float(raw.get('PowerAverage'))  # watts

    swim_pace = None
    run_pace = None
    mph = None
    sport_lc = (workout.sport or "").lower()
    
    # Calculate pace/speed from actual distance and duration
    if normalized_distance and duration_seconds:
        if sport_lc == "swim":
            swim_pace = duration_seconds / (normalized_distance / 100)
        elif sport_lc == "run":
            run_pace = duration_seconds / max(normalized_distance, 1e-6)
            mph = normalized_distance / (duration_seconds / 3600)
        elif sport_lc == "bike":
            mph = normalized_distance / (duration_seconds / 3600)
    
    # Convert VelocityAverage from m/s to mph if available
    if avg_speed and not mph:
        mph = avg_speed * 2.23694  # m/s to mph

    return {
        "duration_seconds": duration_seconds,
        "distance_value": normalized_distance,
        "distance_unit": distance_unit,
        "average_speed_mph": mph,
        "swim_pace_sec_per_100": swim_pace,
        "run_pace_sec_per_mile": run_pace,
        "power_watts": avg_power,
        "completed": True,
    }


def _metric_entry(
    name: str,
    planned: Optional[float],
    actual: Optional[float],
    unit: str,
    rating: Optional[str],
    detail: Optional[float] = None,
    include_in_score: bool = True,
) -> Dict[str, Any]:
    return {
        "metric": name,
        "planned": planned,
        "actual": actual,
        "unit": unit,
        "rating": rating,
        "delta": detail,
        "planned_raw": planned,
        "actual_raw": actual,
        "include_in_score": include_in_score,
    }


def _decorate_metrics(
    metrics: List[Dict[str, Any]],
    sport: str,
    swim_distance_unit: Optional[str] = None,
) -> None:
    sport_lc = (sport or "").lower()
    if sport_lc == "swim":
        distance_unit = (swim_distance_unit or "yd")
        pace_unit = "min/100 m" if distance_unit == "m" else "min/100 yd"
    else:
        distance_unit = "mi" if sport_lc in {"run", "bike"} else "units"
        pace_unit = "min/mi" if sport_lc == "run" else "min"

    for entry in metrics:
        metric = entry.get("metric")
        planned_raw = entry.get("planned_raw")
        actual_raw = entry.get("actual_raw")

        if metric == "distance":
            entry["unit"] = distance_unit
            entry["planned"] = _round_distance(planned_raw, sport_lc) if planned_raw is not None else None
            entry["actual"] = _round_distance(actual_raw, sport_lc) if actual_raw is not None else None
            entry["delta"] = _percent_to_display(entry.get("delta"))
        elif metric == "duration":
            entry["unit"] = "min"
            entry["planned"] = _seconds_to_minutes(planned_raw)
            entry["actual"] = _seconds_to_minutes(actual_raw)
            entry["delta"] = _percent_to_display(entry.get("delta"))
        elif metric == "pace":
            entry["unit"] = pace_unit
            entry["planned"] = _seconds_to_time_string(planned_raw)
            entry["actual"] = _seconds_to_time_string(actual_raw)
            pace_delta = entry.get("delta")
            entry["delta"] = _seconds_to_time_string(pace_delta) if isinstance(pace_delta, (int, float)) else pace_delta
        elif metric == "speed":
            entry["unit"] = "mph"
            entry["planned"] = round(planned_raw, 1) if planned_raw is not None else None
            entry["actual"] = round(actual_raw, 1) if actual_raw is not None else None
            entry["delta"] = _percent_to_display(entry.get("delta"))
        elif metric == "power":
            entry["unit"] = "W"
            entry["planned"] = round(planned_raw, 0) if planned_raw is not None else None
            entry["actual"] = round(actual_raw, 0) if actual_raw is not None else None


def _evaluate_swim(planned: Dict[str, Any], actual: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics: List[Dict[str, Any]] = []
    rating_distance = _rate_percent(
        _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        DISTANCE_GOOD_PCT,
        DISTANCE_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "distance",
            planned.get("distance_value"),
            actual.get("distance_value"),
            "raw",
            rating_distance,
            _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        )
    )

    # Duration and pace are still displayed but NOT included in compliance score
    rating_duration = _rate_percent(
        _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
        DURATION_GOOD_PCT,
        DURATION_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "duration",
            planned.get("duration_seconds"),
            actual.get("duration_seconds"),
            "seconds",
            rating_duration,
            _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
            include_in_score=False,  # Display only, not included in compliance score
        )
    )

    planned_pace = planned.get("swim_pace_sec_per_100")
    actual_pace = actual.get("swim_pace_sec_per_100")
    pace_diff = None
    if planned_pace is not None and actual_pace is not None:
        pace_diff = abs(actual_pace - planned_pace)
    rating_pace = _rate_abs(pace_diff, SWIM_PACE_THRESHOLDS)
    metrics.append(
        _metric_entry(
            "pace",
            planned_pace,
            actual_pace,
            "sec/100",
            rating_pace,
            pace_diff,
            include_in_score=False,  # Display only, not included in compliance score
        )
    )
    # Use the planned unit if present so swim distance/pace label matches our calculation.
    swim_distance_unit = planned.get("distance_unit") or actual.get("distance_unit")
    _decorate_metrics(metrics, "swim", swim_distance_unit=swim_distance_unit)
    return metrics


def _evaluate_run(planned: Dict[str, Any], actual: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics: List[Dict[str, Any]] = []
    rating_distance = _rate_percent(
        _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        DISTANCE_GOOD_PCT,
        DISTANCE_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "distance",
            planned.get("distance_value"),
            actual.get("distance_value"),
            "raw",
            rating_distance,
            _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        )
    )

    rating_duration = _rate_percent(
        _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
        DURATION_GOOD_PCT,
        DURATION_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "duration",
            planned.get("duration_seconds"),
            actual.get("duration_seconds"),
            "seconds",
            rating_duration,
            _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
        )
    )

    planned_pace = planned.get("run_pace_sec_per_mile")
    actual_pace = actual.get("run_pace_sec_per_mile")
    pace_diff = None
    if planned_pace is not None and actual_pace is not None:
        pace_diff = abs(actual_pace - planned_pace)
    rating_pace = _rate_abs(pace_diff, RUN_PACE_THRESHOLDS)
    metrics.append(
        _metric_entry(
            "pace",
            planned_pace,
            actual_pace,
            "sec/mile",
            rating_pace,
            pace_diff,
        )
    )
    _decorate_metrics(metrics, "run")
    return metrics


def _evaluate_bike(planned: Dict[str, Any], actual: Dict[str, Any]) -> List[Dict[str, Any]]:
    metrics: List[Dict[str, Any]] = []
    rating_distance = _rate_percent(
        _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        DISTANCE_GOOD_PCT,
        DISTANCE_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "distance",
            planned.get("distance_value"),
            actual.get("distance_value"),
            "raw",
            rating_distance,
            _percent_delta(planned.get("distance_value"), actual.get("distance_value")),
        )
    )

    rating_duration = _rate_percent(
        _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
        DURATION_GOOD_PCT,
        DURATION_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "duration",
            planned.get("duration_seconds"),
            actual.get("duration_seconds"),
            "seconds",
            rating_duration,
            _percent_delta(planned.get("duration_seconds"), actual.get("duration_seconds")),
        )
    )

    rating_speed = _rate_percent(
        _percent_delta(planned.get("average_speed_mph"), actual.get("average_speed_mph")),
        DISTANCE_GOOD_PCT,
        DISTANCE_OK_PCT,
    )
    metrics.append(
        _metric_entry(
            "speed",
            planned.get("average_speed_mph"),
            actual.get("average_speed_mph"),
            "mph",
            rating_speed,
            _percent_delta(planned.get("average_speed_mph"), actual.get("average_speed_mph")),
        )
    )

    planned_power = planned.get("power_watts")
    actual_power = actual.get("power_watts")
    power_diff = None
    if planned_power is not None and actual_power is not None:
        power_diff = abs(actual_power - planned_power)
    rating_power = _rate_abs(power_diff, BIKE_POWER_THRESHOLDS)
    metrics.append(
        _metric_entry(
            "power",
            planned_power,
            actual_power,
            "watts",
            rating_power,
            power_diff,
        )
    )
    _decorate_metrics(metrics, "bike")
    return metrics


def _score_metrics(metrics: List[Dict[str, Any]]) -> Optional[float]:
    scores = [
        RATING_TO_SCORE[m["rating"]] 
        for m in metrics 
        if m.get("rating") in RATING_TO_SCORE and m.get("include_in_score", True)
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def _build_notes(metrics: List[Dict[str, Any]]) -> Optional[str]:
    flagged = [m for m in metrics if m.get("rating") and m["rating"] != "good"]
    if not flagged:
        return None
    parts = [f"{m['metric']} rating {m['rating']}" for m in flagged]
    return "; ".join(parts)


def evaluate_workout_compliance(
    workout: Workout,
) -> Optional[Dict[str, Any]]:
    """Return a compliance summary for a workout (without storing).
    
    Extracts planned and actual values from workout.raw_json.
    No external plan_data needed - everything is in the TrainingPeaks workout object.
    """
    if workout is None:
        return None
    sport = (workout.sport or "").lower()
    raw = workout.raw_json or {}
    
    # Extract planned and actual from same source (TrainingPeaks workout object)
    planned_summary = _collect_plan_summary(sport, raw)
    preferred_swim_unit = None
    if sport == "swim":
        preferred_swim_unit = planned_summary.get("distance_unit")
        # If the plan doesn't specify distance, infer from the actual distance.
        if planned_summary.get("distance_value") is None:
            preferred_swim_unit = _infer_swim_distance_unit_from_meters(_as_float(raw.get("Distance")))
    actual_summary = _collect_actual_summary(workout, preferred_swim_unit=preferred_swim_unit)

    metrics: List[Dict[str, Any]]
    if sport == "swim":
        metrics = _evaluate_swim(planned_summary, actual_summary)
    elif sport == "run":
        metrics = _evaluate_run(planned_summary, actual_summary)
    elif sport == "bike":
        metrics = _evaluate_bike(planned_summary, actual_summary)
    else:
        # For now only evaluate swim, run, bike. Other sports can be handled later.
        metrics = []

    score = _score_metrics(metrics)
    notes = _build_notes(metrics)
    return {
        "sport": sport,
        "planned": planned_summary,
        "actual": actual_summary,
        "metrics": metrics,
        "overall_score": score,
        "notes": notes,
    }


def upsert_workout_compliance(
    session: Session,
    workout: Workout,
) -> Optional[Dict[str, Any]]:
    """Calculate and store workout compliance.
    
    All data comes from workout.raw_json - no external plan_data needed.
    """
    summary = evaluate_workout_compliance(workout)
    if summary is None:
        return None
    metrics = summary.get("metrics") or []
    if not metrics:
        return summary

    stmt = select(WorkoutCompliance).where(WorkoutCompliance.workout_id == workout.id)
    existing = session.execute(stmt).scalars().first()
    if existing is None:
        existing = WorkoutCompliance(
            workout_id=workout.id,
            athlete_id=workout.athlete_id,
            workout_date=workout.date,
            sport=workout.sport,
        )
        session.add(existing)

    existing.planned_summary = summary.get("planned")
    existing.actual_summary = summary.get("actual")
    existing.metrics = metrics
    existing.overall_score = summary.get("overall_score")
    existing.evaluation_notes = summary.get("notes")
    existing.sport = workout.sport
    return summary


def get_compliance_for_day(athlete_id: int, target_date: date) -> Optional[Dict[str, Any]]:
    with get_session() as session:
        def _serialize(record: WorkoutCompliance, workout: Optional[Workout]) -> Dict[str, Any]:
            return {
                "athlete_id": record.athlete_id,
                "workout_id": record.workout_id,
                "workout_date": record.workout_date.isoformat() if record.workout_date else None,
                "sport": record.sport,
                "workout_title": _extract_workout_title((workout.raw_json if workout else None)),
                "planned": record.planned_summary,
                "actual": record.actual_summary,
                "metrics": record.metrics,
                "overall_score": record.overall_score,
                "notes": record.evaluation_notes,
                "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            }

        exact_stmt = (
            select(WorkoutCompliance, Workout)
            .join(Workout, Workout.id == WorkoutCompliance.workout_id, isouter=True)
            .where(WorkoutCompliance.athlete_id == athlete_id)
            .where(WorkoutCompliance.workout_date == target_date)
            .order_by(WorkoutCompliance.updated_at.desc())
        )

        rows = session.execute(exact_stmt).all()
        exact_records = [row[0] for row in rows]
        
        # Return None if no workouts found for the target date (no fallback)
        if not exact_records:
            return None

        by_workout_id: dict[int, Optional[Workout]] = {}
        for record, workout in rows:
            if record and record.workout_id is not None:
                by_workout_id[int(record.workout_id)] = workout

        return {
            "requested_date": target_date.isoformat(),
            "is_exact_match": True,
            "workout_date": target_date.isoformat(),
            "records": [_serialize(record, by_workout_id.get(int(record.workout_id))) for record in exact_records],
        }


def get_compliance_for_range(
    athlete_id: int,
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    """Return workout compliance records across an inclusive date range."""
    with get_session() as session:
        def _serialize(record: WorkoutCompliance, workout: Optional[Workout]) -> Dict[str, Any]:
            return {
                "athlete_id": record.athlete_id,
                "workout_id": record.workout_id,
                "workout_date": record.workout_date.isoformat() if record.workout_date else None,
                "sport": record.sport,
                "workout_title": _extract_workout_title((workout.raw_json if workout else None)),
                "planned": record.planned_summary,
                "actual": record.actual_summary,
                "metrics": record.metrics,
                "overall_score": record.overall_score,
                "notes": record.evaluation_notes,
                "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            }

        stmt = (
            select(WorkoutCompliance, Workout)
            .join(Workout, Workout.id == WorkoutCompliance.workout_id, isouter=True)
            .where(WorkoutCompliance.athlete_id == athlete_id)
            .where(WorkoutCompliance.workout_date >= start_date)
            .where(WorkoutCompliance.workout_date <= end_date)
            .order_by(WorkoutCompliance.workout_date.desc(), WorkoutCompliance.updated_at.desc())
        )

        rows = session.execute(stmt).all()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "records": [_serialize(record, workout) for record, workout in rows],
    }