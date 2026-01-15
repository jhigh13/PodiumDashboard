from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

from sqlalchemy import func, select

from app.data.db import get_session
from app.models.tables import DailyMetric, Workout
from app.services.compliance import evaluate_workout_compliance


def _sport_key(raw: str | None) -> str:
    return (raw or "").strip().lower()


def compute_leadup_training_stats(
    athlete_id: int,
    race_date: date,
    window_days: int = 28,
) -> Dict[str, float | int]:
    """Compute average weekly training volumes in the days leading to a race.

    - Run: miles/week (actual distance only)
    - Swim: yards/week (actual distance only)
    - Bike: hours/week (actual duration only)

    Only completed workouts contribute to actual values.
    """
    start = race_date - timedelta(days=window_days)
    end = race_date - timedelta(days=1)

    run_miles = 0.0
    swim_yards = 0.0
    bike_hours = 0.0
    counts = {"run": 0, "swim": 0, "bike": 0}

    with get_session() as session:
        stmt = (
            select(Workout)
            .where(Workout.athlete_id == athlete_id)
            .where(Workout.date >= start)
            .where(Workout.date <= end)
        )
        workouts = session.execute(stmt).scalars().all()

    for w in workouts:
        sport = _sport_key(w.sport)
        if sport not in {"run", "swim", "bike"}:
            continue
        summary = evaluate_workout_compliance(w) or {}
        actual = summary.get("actual") or {}
        completed = actual.get("completed") is True
        if not completed:
            continue

        if sport == "run":
            miles = actual.get("distance_value")
            if isinstance(miles, (int, float)):
                run_miles += float(miles)
                counts["run"] += 1
        elif sport == "swim":
            yards = actual.get("distance_value")
            if isinstance(yards, (int, float)):
                swim_yards += float(yards)
                counts["swim"] += 1
        elif sport == "bike":
            dur_sec = actual.get("duration_seconds")
            if isinstance(dur_sec, (int, float)) and dur_sec > 0:
                bike_hours += float(dur_sec) / 3600.0
                counts["bike"] += 1

    weeks = max(window_days / 7.0, 1.0)
    return {
        "window_days": window_days,
        "start_date": start,
        "end_date": end,
        "run_miles_per_week": run_miles / weeks,
        "swim_yards_per_week": swim_yards / weeks,
        "bike_hours_per_week": bike_hours / weeks,
        "workout_counts": counts,
    }


def compute_leadup_total_training_hours_per_week(
    athlete_id: int,
    race_date: date,
    window_days: int = 28,
) -> Dict[str, float | int | date | None]:
    """Compute total training time as average hours/week over the lead-up window.

    Uses stored `Workout.duration_sec` and aggregates in SQL for efficiency.
    """
    start = race_date - timedelta(days=window_days)
    end = race_date - timedelta(days=1)

    with get_session() as session:
        stmt = (
            select(func.sum(Workout.duration_sec))
            .where(Workout.athlete_id == athlete_id)
            .where(Workout.date >= start)
            .where(Workout.date <= end)
            .where(Workout.duration_sec.isnot(None))
            .where(Workout.duration_sec > 0)
        )
        total_sec = session.execute(stmt).scalar_one_or_none()

    total_sec_f = float(total_sec or 0.0)
    weeks = max(window_days / 7.0, 1.0)
    return {
        "window_days": window_days,
        "start_date": start,
        "end_date": end,
        "total_hours_per_week": (total_sec_f / 3600.0) / weeks,
    }


def compute_leadup_average_sleep_hours(
    athlete_id: int,
    race_date: date,
    window_days: int = 28,
) -> Dict[str, float | int | date | None]:
    """Compute average sleep (hours) over the lead-up window.

    Uses stored `DailyMetric.sleep_hours` and aggregates in SQL.
    """
    start = race_date - timedelta(days=window_days)
    end = race_date - timedelta(days=1)

    with get_session() as session:
        stmt = (
            select(func.avg(DailyMetric.sleep_hours))
            .where(DailyMetric.athlete_id == athlete_id)
            .where(DailyMetric.date >= start)
            .where(DailyMetric.date <= end)
            .where(DailyMetric.sleep_hours.isnot(None))
        )
        avg_sleep = session.execute(stmt).scalar_one_or_none()

    return {
        "window_days": window_days,
        "start_date": start,
        "end_date": end,
        "avg_sleep_hours": float(avg_sleep) if isinstance(avg_sleep, (int, float)) else None,
    }
