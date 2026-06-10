"""
Strength of Field (SoF) scoring service.

Loads the historical SoF reference distribution (built by triathlon-db's
`scripts/build_sof_reference.py`) and exposes `compute_field_sof()` which
maps a field's raw metrics to 0-100 scores via empirical percentile.

Three metrics, all higher-is-stronger:
    Elo:    mean Elo of the 10 best-Elo athletes in the field
    WT:     mean WT rank of the 10 best WT-ranked athletes (inverted)
    Depth:  count of athletes ranked top-50 globally

Computed per gender (Elite Men / Elite Women).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Reference file path — lives in triathlon-db sibling repo
TRIATHLON_DB_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "triathlon-db")
)
REFERENCE_PATH = os.path.join(TRIATHLON_DB_ROOT, "outputs", "sof_reference.json")

# ranking_cat_id mapping in triathlon-db's computed_weekly_rankings table
GENDER_CAT_ID = {"men": 13, "women": 14}

# Lazy-loaded reference (built once per process)
_reference_cache: dict | None = None


def _load_reference() -> dict | None:
    """Load the historical reference JSON. Cached after first call."""
    global _reference_cache
    if _reference_cache is not None:
        return _reference_cache
    if not os.path.exists(REFERENCE_PATH):
        logger.warning(f"SoF reference file not found at {REFERENCE_PATH}")
        return None
    try:
        with open(REFERENCE_PATH) as f:
            _reference_cache = json.load(f)
        logger.info(f"Loaded SoF reference: {REFERENCE_PATH}")
        return _reference_cache
    except Exception as e:
        logger.warning(f"Could not load SoF reference: {e}")
        return None


def _value_to_score(value: float, percentiles: list, higher_is_better: bool) -> int:
    """
    Map a raw value to a 0-100 percentile score using the empirical CDF.

    `percentiles` is a list of 101 raw values at percentile 0, 1, 2, ..., 100,
    sorted ascending. We find which bucket `value` falls into and return that
    percentile (inverting if lower-is-better).
    """
    if value is None or pd.isna(value) or not percentiles:
        return 0
    # Drop None entries (defensive)
    clean = [p for p in percentiles if p is not None]
    if not clean:
        return 0

    # Find the largest percentile index whose value is <= our value
    pct = 0
    for i, p in enumerate(clean):
        if value >= p:
            pct = i
        else:
            break

    # Scale back to 0-100 if clean had fewer than 101 entries
    pct = int(round(pct * 100 / (len(clean) - 1))) if len(clean) > 1 else 0

    if not higher_is_better:
        pct = 100 - pct
    return max(0, min(100, pct))


def _gender_from_prog_name(prog_name: str | None) -> str:
    name = (prog_name or "").lower()
    if "women" in name or "female" in name:
        return "women"
    return "men"


def _fetch_field_signals(
    tri_engine: Engine,
    athlete_ids: list[int],
    gender: str,
    event_date,
) -> pd.DataFrame:
    """
    For the list of athletes, fetch current Elo and the most-recent WT rank
    at or before event_date. Returns DataFrame with columns:
        athlete_id, elo_rating, wt_rank_position
    """
    if not athlete_ids:
        return pd.DataFrame(columns=["athlete_id", "elo_rating", "wt_rank_position"])

    g_variants = {
        "men":   ("male", "men"),
        "women": ("female", "women"),
    }
    cat_id = GENDER_CAT_ID.get(gender, 13)
    g1, g2 = g_variants.get(gender, ("male", "men"))

    with tri_engine.connect() as conn:
        # Elo (current snapshot)
        elo = pd.read_sql(text("""
            SELECT athlete_id, elo_rating
            FROM athlete_elo_ratings
            WHERE athlete_id = ANY(:ids)
              AND LOWER(gender) IN (:g1, :g2)
              AND elo_rating IS NOT NULL
        """), conn, params={"ids": list(athlete_ids), "g1": g1, "g2": g2})

        # WT rank: most-recent ranking <= event_date per athlete
        wt = pd.read_sql(text("""
            SELECT DISTINCT ON (athlete_id) athlete_id, rank_position
            FROM computed_weekly_rankings
            WHERE athlete_id = ANY(:ids)
              AND ranking_cat_id = :cat
              AND ranking_date <= :ed
            ORDER BY athlete_id, ranking_date DESC
        """), conn, params={"ids": list(athlete_ids), "cat": cat_id, "ed": event_date})

    df = pd.DataFrame({"athlete_id": list(athlete_ids)})
    df = df.merge(elo, on="athlete_id", how="left")
    df = df.merge(wt.rename(columns={"rank_position": "wt_rank_position"}),
                  on="athlete_id", how="left")
    return df


def compute_field_sof(
    tri_engine: Engine,
    athlete_ids: list[int],
    prog_name: str | None,
    event_date,
) -> dict:
    """
    Compute Strength of Field scores for a race.

    Args:
        tri_engine: SQLAlchemy engine for the triathlon-db database.
        athlete_ids: List of athlete_ids in the start list (or finishers).
        prog_name: Program name (used to pick Elite Men vs Elite Women reference).
        event_date: The race date (used to look up historical WT rank).

    Returns:
        Dict with keys:
            scores: {"elo": int, "wt": int, "depth": int}  # all 0-100
            raw:    {"mean_elo_top10": float, "mean_wt_rank_top10": float, "n_top50_world_ranked": int}
            n_athletes:     int
            n_with_elo:     int
            n_with_wt_rank: int
            gender:         "men" | "women"
            note:           str  # description / caveat
    """
    gender = _gender_from_prog_name(prog_name)
    reference = _load_reference()

    df = _fetch_field_signals(tri_engine, athlete_ids, gender, event_date)
    elo_vals = df["elo_rating"].dropna()
    wt_vals = df["wt_rank_position"].dropna()

    elo_top10 = elo_vals.nlargest(10)
    wt_top10 = wt_vals.nsmallest(10)
    n_top50 = int((wt_vals <= 50).sum())

    raw = {
        "mean_elo_top10": float(elo_top10.mean()) if len(elo_top10) > 0 else None,
        "mean_wt_rank_top10": float(wt_top10.mean()) if len(wt_top10) > 0 else None,
        "n_top50_world_ranked": n_top50,
    }

    scores = {"elo": 0, "wt": 0, "depth": 0}
    if reference and gender in reference:
        metrics = reference[gender]["metrics"]
        scores["elo"] = _value_to_score(
            raw["mean_elo_top10"],
            metrics["mean_elo_top10"]["percentiles"],
            metrics["mean_elo_top10"]["higher_is_better"],
        )
        scores["wt"] = _value_to_score(
            raw["mean_wt_rank_top10"],
            metrics["mean_wt_rank_top10"]["percentiles"],
            metrics["mean_wt_rank_top10"]["higher_is_better"],
        )
        scores["depth"] = _value_to_score(
            float(raw["n_top50_world_ranked"]),
            metrics["n_top50_world_ranked"]["percentiles"],
            metrics["n_top50_world_ranked"]["higher_is_better"],
        )

    return {
        "scores": scores,
        "raw": raw,
        "n_athletes": len(athlete_ids),
        "n_with_elo": int(len(elo_vals)),
        "n_with_wt_rank": int(len(wt_vals)),
        "gender": gender,
        "note": "Elo from current snapshot; WT rank historical as of event_date",
    }
