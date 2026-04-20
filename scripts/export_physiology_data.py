"""Export race results and daily metrics to CSV for the physiology experiment.

Creates two files in outputs/:
  - races.csv       – one row per race per mapped athlete
  - daily_metrics.csv – one row per athlete per day (sleep, HRV, RHR, training load)

Usage:
    python scripts/export_physiology_data.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import func, select, text

# Ensure project root on sys.path so ``app`` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.db import get_session
from app.data.triathlon_db import get_triathlon_engine
from app.models.tables import (
    Athlete,
    DailyMetric,
    Workout,
    WTODashboardAthleteMap,
    WTORaceResult,
)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"


# ---------------------------------------------------------------------------
# Race category helper (simplified from triathlon-db tier logic)
# ---------------------------------------------------------------------------
def _classify_race_category(event_name: str) -> str:
    """Derive a human-readable race category from the event name."""
    name = (event_name or "").lower()
    if "championship" in name or "games" in name or "olympic" in name:
        return "championship"
    if "championship series" in name or "wtcs" in name:
        return "wtcs"
    if "t100" in name:
        return "t100"
    if "world cup" in name:
        return "worldcup"
    if "continental" in name or "americas" in name or "europe" in name or "asia" in name or "africa" in name or "oceania" in name:
        return "continental_cup"
    return "other"


# ---------------------------------------------------------------------------
# Export races
# ---------------------------------------------------------------------------
def export_races() -> pd.DataFrame:
    """Build races.csv by joining local WTORaceResult with triathlon-db for field sizes."""

    # 1. Load local race results for all mapped athletes
    with get_session() as session:
        rows = (
            session.execute(
                select(
                    WTORaceResult.podium_athlete_id,
                    Athlete.name.label("athlete_name"),
                    WTORaceResult.event_id,
                    WTORaceResult.prog_id,
                    WTORaceResult.event_date,
                    WTORaceResult.event_name,
                    WTORaceResult.prog_name,
                    WTORaceResult.prog_distance_category,
                    WTORaceResult.finish_status,
                    WTORaceResult.finish_position,
                    WTORaceResult.total_time,
                    WTORaceResult.swim_time,
                    WTORaceResult.bike_time,
                    WTORaceResult.run_time,
                )
                .join(Athlete, Athlete.id == WTORaceResult.podium_athlete_id)
                .order_by(WTORaceResult.podium_athlete_id, WTORaceResult.event_date)
            )
            .all()
        )

    if not rows:
        print("  No race results found in local DB.")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "athlete_id", "athlete_name", "event_id", "prog_id",
        "event_date", "event_name", "prog_name", "distance",
        "finish_status", "finish_position", "total_time",
        "swim_time", "bike_time", "run_time",
    ])

    # 2. Get field sizes from triathlon-db (full race entry counts)
    engine = get_triathlon_engine()
    if engine is not None:
        event_prog_pairs = df[["event_id", "prog_id"]].drop_duplicates()
        eids = event_prog_pairs["event_id"].tolist()
        pids = event_prog_pairs["prog_id"].tolist()

        sql = text("""
            SELECT event_id, prog_id, COUNT(*) AS field_size
            FROM public.race_results
            WHERE (event_id, prog_id) IN (
                SELECT unnest(:eids), unnest(:pids)
            )
            GROUP BY event_id, prog_id
        """)

        with engine.connect() as conn:
            fs_rows = conn.execute(sql, {"eids": eids, "pids": pids}).fetchall()

        fs_df = pd.DataFrame(fs_rows, columns=["event_id", "prog_id", "field_size"])
        df = df.merge(fs_df, on=["event_id", "prog_id"], how="left")
    else:
        print("  WARNING: triathlon-db not available — field_size will be null.")
        df["field_size"] = None

    # 3. Derive columns
    df["race_category"] = df["event_name"].apply(_classify_race_category)
    df["finish_pct"] = pd.to_numeric(df["finish_position"], errors="coerce") / pd.to_numeric(df["field_size"], errors="coerce")

    # 4. Select final columns
    out = df[[
        "athlete_id", "athlete_name", "event_date", "event_name",
        "race_category", "distance", "finish_status", "finish_position",
        "field_size", "finish_pct", "total_time", "swim_time", "bike_time", "run_time",
    ]].copy()

    return out


# ---------------------------------------------------------------------------
# Export daily metrics
# ---------------------------------------------------------------------------
def export_daily_metrics() -> pd.DataFrame:
    """Build daily_metrics.csv with physiology + daily training aggregates."""

    with get_session() as session:
        # Daily metrics (HRV, RHR, sleep, CTL/ATL/TSB)
        metric_rows = (
            session.execute(
                select(
                    DailyMetric.athlete_id,
                    DailyMetric.date,
                    DailyMetric.sleep_hours,
                    DailyMetric.rhr,
                    DailyMetric.hrv,
                    DailyMetric.ctl,
                    DailyMetric.atl,
                    DailyMetric.tsb,
                )
                .order_by(DailyMetric.athlete_id, DailyMetric.date)
            )
            .all()
        )

        # Workout aggregates per athlete per day
        workout_rows = (
            session.execute(
                select(
                    Workout.athlete_id,
                    Workout.date,
                    func.count(Workout.id).label("num_workouts"),
                    func.sum(Workout.tss).label("total_tss"),
                    func.sum(Workout.duration_sec).label("total_duration_sec"),
                )
                .group_by(Workout.athlete_id, Workout.date)
                .order_by(Workout.athlete_id, Workout.date)
            )
            .all()
        )

    metrics_df = pd.DataFrame(metric_rows, columns=[
        "athlete_id", "date", "sleep_hours", "rhr", "hrv", "ctl", "atl", "tsb",
    ])

    workouts_df = pd.DataFrame(workout_rows, columns=[
        "athlete_id", "date", "num_workouts", "total_tss", "total_duration_sec",
    ])

    # Merge on athlete + date
    df = metrics_df.merge(workouts_df, on=["athlete_id", "date"], how="outer")
    df = df.sort_values(["athlete_id", "date"]).reset_index(drop=True)

    # Convert duration to hours
    df["total_duration_hrs"] = pd.to_numeric(df["total_duration_sec"], errors="coerce") / 3600.0
    df = df.drop(columns=["total_duration_sec"])

    # Fill workout columns to 0 for days with metrics but no workouts
    df["num_workouts"] = df["num_workouts"].fillna(0).astype(int)
    df["total_tss"] = df["total_tss"].fillna(0.0)
    df["total_duration_hrs"] = df["total_duration_hrs"].fillna(0.0)

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("Exporting races...")
    races = export_races()
    races_path = OUTPUT_DIR / "races.csv"
    races.to_csv(races_path, index=False)
    print(f"  {len(races)} rows -> {races_path}")

    print("Exporting daily metrics...")
    metrics = export_daily_metrics()
    metrics_path = OUTPUT_DIR / "daily_metrics.csv"
    metrics.to_csv(metrics_path, index=False)
    print(f"  {len(metrics)} rows -> {metrics_path}")

    print("Done!")


if __name__ == "__main__":
    main()
