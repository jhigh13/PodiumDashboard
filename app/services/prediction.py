"""
Prediction service: bridges PodiumDashboard with triathlon-db's ML pipeline.

Provides functions for:
- Fetching upcoming events with start lists
- Running the full prediction + simulation pipeline
- Re-simulating with adjusted parameters (using cached features)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.data.triathlon_db import get_triathlon_engine

logger = logging.getLogger(__name__)

# ── Model Bundle Cache ──────────────────────────────────────────────
# Loaded once on first use, keyed by gender
_bundle_cache: dict[str, object] = {}

# ── Prediction Cache ────────────────────────────────────────────────
# Caches the deterministic pred_df so re-simulation only reruns Monte Carlo.
# Key: (event_id, prog_id, athlete_ids_tuple), Value: (pred_df, distance_category, gender)
_pred_cache: dict[tuple, tuple[pd.DataFrame, str | None, str]] = {}

# Model bundle paths — triathlon-db is a sibling repo
TRIATHLON_DB_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "triathlon-db")
)
MODEL_PATHS = {
    "men": os.path.join(TRIATHLON_DB_ROOT, "models", "bundle_claudev24_men.joblib"),
    "women": os.path.join(TRIATHLON_DB_ROOT, "models", "bundle_claudev24_women.joblib"),
}


def _get_bundle(gender: str):
    """Load and cache model bundle for the given gender."""
    from tri_analysis.prediction.train import load_model_bundle

    key = gender.lower()
    if key not in _bundle_cache:
        path = MODEL_PATHS.get(key)
        if not path or not os.path.exists(path):
            raise FileNotFoundError(
                f"Model bundle not found for gender={key}: {path}"
            )
        _bundle_cache[key] = load_model_bundle(path)
        logger.info(f"Loaded model bundle: {path}")
    return _bundle_cache[key]


def _gender_from_prog_name(prog_name: str) -> str:
    """Determine gender from program name (e.g. 'Elite Women' -> 'women')."""
    name_lower = (prog_name or "").lower()
    if "women" in name_lower or "female" in name_lower:
        return "women"
    return "men"


# ── Event / Program Queries ─────────────────────────────────────────


def fetch_upcoming_events(engine: Engine, days_ahead: int = 180) -> list[dict]:
    """Fetch upcoming events that have active start lists."""
    query = text("""
        SELECT DISTINCT
            e.event_id,
            e.event_name,
            e.event_date,
            e.event_venue,
            e.event_country
        FROM events e
        JOIN program_entries pe ON pe.event_id = e.event_id
        WHERE e.event_date >= CURRENT_DATE - INTERVAL '30 days'
          AND e.event_date <= CURRENT_DATE + :days_ahead * INTERVAL '1 day'
          AND pe.is_active = TRUE
          AND pe.entry_type = 'start'
        ORDER BY e.event_date ASC
    """)
    with engine.connect() as conn:
        rows = conn.execute(query, {"days_ahead": days_ahead}).mappings().all()
    return [dict(r) for r in rows]


def fetch_event_programs(engine: Engine, event_id: int) -> list[dict]:
    """Fetch available programs (Men Elite, Women Elite, etc.) for an event."""
    query = text("""
        SELECT DISTINCT
            e.event_id,
            e.prog_id,
            e.prog_name,
            e.prog_distance_category
        FROM events e
        JOIN program_entries pe
            ON pe.event_id = e.event_id AND pe.prog_id = e.prog_id
        WHERE e.event_id = :event_id
          AND pe.is_active = TRUE
          AND pe.entry_type = 'start'
        ORDER BY e.prog_name
    """)
    with engine.connect() as conn:
        rows = conn.execute(query, {"event_id": event_id}).mappings().all()
    return [dict(r) for r in rows]


# ── Start List / Athlete Search ────────────────────────────────────


def fetch_start_list_for_program(event_id: int, prog_id: int) -> list[dict]:
    """Fetch the start list for a program, returning athlete info for UI display."""
    from tri_analysis.prediction.sql import ProgramKey, fetch_start_list

    tri_engine = get_triathlon_engine()
    if tri_engine is None:
        return []
    key = ProgramKey(event_id=event_id, prog_id=prog_id)
    df = fetch_start_list(tri_engine, key)
    if df.empty:
        return []
    return df[["athlete_id", "athlete_full_name", "athlete_country_name"]].to_dict(orient="records")


def search_athletes_in_triathlon_db(query: str, exclude_ids: list[int] | None = None) -> list[dict]:
    """Search athletes by name for the add-athlete UI."""
    from tri_analysis.prediction.sql import search_athletes

    tri_engine = get_triathlon_engine()
    if tri_engine is None:
        return []
    df = search_athletes(tri_engine, query, limit=15, exclude_ids=exclude_ids)
    if df.empty:
        return []
    return df.to_dict(orient="records")


# ── Prediction Pipeline ─────────────────────────────────────────────


def run_prediction_pipeline(
    event_id: int,
    prog_id: int,
    breakaway_bias: float = 0.0,
    form_share: float = 0.2,
    n_sims: int = 10000,
    athlete_ids: list[int] | None = None,
) -> dict:
    """
    Run the full prediction + Monte Carlo pipeline.

    Returns dict with keys:
        display_df, event_meta, n_athletes, error,
        breakaway_bias, form_share
    """
    from tri_analysis.prediction.sql import ProgramKey, fetch_event_metadata
    from tri_analysis.prediction.features import (
        build_features_for_program,
        fill_missing_features,
    )
    from tri_analysis.prediction.predict import predict_splits_and_total
    from tri_analysis.prediction.simulate import (
        run_monte_carlo,
        format_simulation_output,
        get_distance_pack_params,
        get_distance_merge_params,
        PackEffectParams,
    )

    tri_engine = get_triathlon_engine()
    if tri_engine is None:
        return {"error": "TRIATHLON_DATABASE_URL is not configured.", "display_df": None}

    key = ProgramKey(event_id=event_id, prog_id=prog_id)

    # Get event metadata
    event_meta = fetch_event_metadata(tri_engine, key)
    if event_meta is None:
        return {
            "error": f"Event not found: event_id={event_id}, prog_id={prog_id}",
            "display_df": None,
        }

    prog_name = event_meta.get("prog_name", "")
    gender = _gender_from_prog_name(prog_name)
    distance_cat = event_meta.get("prog_distance_category")

    # Load model bundle
    try:
        bundle = _get_bundle(gender)
    except FileNotFoundError as e:
        return {"error": str(e), "display_df": None}

    # Build features for start list (or custom athlete list)
    logger.info(f"Building features for event_id={event_id}, prog_id={prog_id}, athlete_ids={athlete_ids}")
    features_df = build_features_for_program(
        tri_engine, key, use_start_list=True,
        athlete_ids_override=athlete_ids,
    )
    if features_df.empty:
        return {"error": "No athletes found in start list.", "display_df": None}

    feature_cols = bundle.feature_columns or []
    features_df = fill_missing_features(features_df, feature_cols)

    # Deterministic predictions
    pred_df = predict_splits_and_total(
        features_df, bundle, distance_category=distance_cat
    )

    # Cache pred_df for re-simulation (include athlete composition in key)
    cache_key = (event_id, prog_id, tuple(sorted(athlete_ids)) if athlete_ids else None)
    _pred_cache[cache_key] = (pred_df.copy(), distance_cat, gender)

    # Extract distance-specific pack/merge params from bundle metadata
    pack_params = get_distance_pack_params(bundle.metadata, distance_cat)
    if pack_params is None:
        pack_params = PackEffectParams(front_pack_bonus_sec=0.0, chase_penalty_sec=0.0)
    merge_params = get_distance_merge_params(bundle.metadata, distance_cat)

    # Monte Carlo simulation
    sim_df = run_monte_carlo(
        pred_df,
        n_sims=n_sims,
        random_state=42,
        pack_params=pack_params,
        merge_params=merge_params,
        breakaway_bias=breakaway_bias,
        form_share=form_share,
        distance_category=distance_cat,
    )

    display_df = format_simulation_output(sim_df)

    return {
        "display_df": display_df,
        "event_meta": event_meta,
        "n_athletes": len(sim_df),
        "error": None,
        "breakaway_bias": breakaway_bias,
        "form_share": form_share,
        "n_sims": n_sims,
    }


def resimulate(
    event_id: int,
    prog_id: int,
    breakaway_bias: float = 0.0,
    form_share: float = 0.2,
    n_sims: int = 10000,
    athlete_ids: list[int] | None = None,
) -> dict:
    """
    Re-run Monte Carlo with adjusted parameters using cached pred_df.
    Falls back to full pipeline on cache miss.
    """
    from tri_analysis.prediction.sql import ProgramKey, fetch_event_metadata
    from tri_analysis.prediction.simulate import (
        run_monte_carlo,
        format_simulation_output,
        get_distance_pack_params,
        get_distance_merge_params,
        PackEffectParams,
    )

    cache_key = (event_id, prog_id, tuple(sorted(athlete_ids)) if athlete_ids else None)
    cached = _pred_cache.get(cache_key)

    if cached is None:
        # Cache miss — run full pipeline
        return run_prediction_pipeline(
            event_id, prog_id, breakaway_bias, form_share, n_sims, athlete_ids
        )

    pred_df, distance_cat, gender = cached

    tri_engine = get_triathlon_engine()
    key = ProgramKey(event_id=event_id, prog_id=prog_id)
    event_meta = fetch_event_metadata(tri_engine, key) if tri_engine else None

    bundle = _get_bundle(gender)
    pack_params = get_distance_pack_params(bundle.metadata, distance_cat)
    if pack_params is None:
        pack_params = PackEffectParams(front_pack_bonus_sec=0.0, chase_penalty_sec=0.0)
    merge_params = get_distance_merge_params(bundle.metadata, distance_cat)

    sim_df = run_monte_carlo(
        pred_df,
        n_sims=n_sims,
        random_state=42,
        pack_params=pack_params,
        merge_params=merge_params,
        breakaway_bias=breakaway_bias,
        form_share=form_share,
        distance_category=distance_cat,
    )

    display_df = format_simulation_output(sim_df)

    return {
        "display_df": display_df,
        "event_meta": event_meta,
        "n_athletes": len(sim_df),
        "error": None,
        "breakaway_bias": breakaway_bias,
        "form_share": form_share,
        "n_sims": n_sims,
    }
