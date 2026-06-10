"""
World Triathlon Ranking expected-points scoring.

Implements section 1.3 (event base points + 7.5%/position decay), 1.4 (8% cut-off
filter), and 1.6 (Continental Championships top-5 bonus) from the World Triathlon
Ranking Criteria (January 2025).

NOT yet implemented:
    - Quality of Field Factor (QoF) — applies to Continental events; formula not in
      the criteria summary, so points for Continental events are "pre-QoF estimates".

The expected points displayed at the dashboard come from integrating over the Monte
Carlo simulation's rank distribution: E[pts] = sum_k P(rank=k) * points(k). We
approximate via bucketed probabilities (Win % / Podium % / Top5 % / Top10 % /
Top20 %) since the decay function is smooth enough that within-bucket averaging is
~accurate.
"""

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)

WT_DECAY = 0.925  # 7.5% reduction per position (section 1.3.c)
CUT_OFF_FACTOR = 1.08  # 8% slower than winner = 0 points (section 1.4)

# Base points per event tier (section 1.3 table)
WT_EVENT_BASE_POINTS = {
    "finals": 1250,
    "olympic_games": 1000,
    "olympic_test": 1000,
    "wtcs": 1000,
    "world_cup": 500,
    "indoor_cup": 500,
    "supertri_e_finals": 500,
    "continental_championships_elite": 400,
    "continental_cup": 250,
    "world_u23_champs": 250,
    "fisu_worlds": 250,
    "supertri_e_series": 250,
    "world_junior_champs": 200,
    "continental_u23_champs": 150,
    "regional_championships": 150,
    "development_regional_cup": 125,
    "continental_junior_champs": 100,
    "national_championships": 50,
    "t100": 0,             # T100 has its own scoring system, not in WT Ranking
    "unknown": 0,
}

# Top-5 bonus multipliers (section 1.6) — Continental Championships Elite only
TOP5_BONUS = {1: 0.25, 2: 0.20, 3: 0.15, 4: 0.10, 5: 0.05}


def classify_event_for_wt_points(
    event_name: str | None, cat_name: str | None
) -> dict:
    """
    Map an event to its WT Ranking scoring tier from its name + category string.

    Returns dict with:
        event_type:           one of WT_EVENT_BASE_POINTS keys
        base_points:          int (0 if event is not in the WT Ranking system)
        top5_bonus_applies:   bool (section 1.6)
        qof_applies:          bool (section 1.3.a — Continental events)
        in_ranking:           bool (False for T100 / unknown)
    """
    n = (event_name or "").lower()
    c = (cat_name or "").lower()

    # Most-specific patterns first
    if "championship finals" in n and ("world triathlon" in n or "wtcs" in n):
        et = "finals"
    elif "olympic games" in n:
        et = "olympic_games"
    elif "olympic test event" in n:
        et = "olympic_test"
    elif "championship series" in n or "wtcs" in n:
        et = "wtcs"
    elif "t100" in n:
        et = "t100"
    elif "supertri" in n and ("championship finals" in n or "finals" in n):
        et = "supertri_e_finals"
    elif "supertri" in n and ("championship series" in n or "series" in n):
        et = "supertri_e_series"
    elif "fisu" in n:
        et = "fisu_worlds"
    # Continental Championships before plain Continental Cup
    elif "continental" in n and "champ" in n and ("elite" in n or "elite" in c) and "u23" not in n and "junior" not in n:
        et = "continental_championships_elite"
    elif "continental" in n and "champ" in n and "u23" in n:
        et = "continental_u23_champs"
    elif "continental" in n and "champ" in n and "junior" in n:
        et = "continental_junior_champs"
    # World U23 / Junior
    elif ("world triathlon u23" in n) or ("world u23 championships" in n):
        et = "world_u23_champs"
    elif ("world triathlon junior" in n) or ("world junior championships" in n):
        et = "world_junior_champs"
    # World Cup (Elite-only, not U23/Junior)
    elif ("world triathlon cup" in n or "world triathlon indoor cup" in n) and "indoor" in n:
        et = "indoor_cup"
    elif "world triathlon cup" in n:
        et = "world_cup"
    # Continental Cup — match regional "Triathlon Cup" patterns
    elif any(
        reg in n
        for reg in [
            "continental cup", "continental triathlon cup",
            "americas triathlon cup", "europe triathlon cup",
            "asia triathlon cup", "africa triathlon cup",
            "oceania triathlon cup", "americas cup", "european cup",
        ]
    ):
        et = "continental_cup"
    elif "regional championships" in n:
        et = "regional_championships"
    elif "development regional cup" in n:
        et = "development_regional_cup"
    elif "national championships" in n:
        et = "national_championships"
    else:
        et = "unknown"

    base = WT_EVENT_BASE_POINTS.get(et, 0)
    return {
        "event_type": et,
        "base_points": base,
        "top5_bonus_applies": et == "continental_championships_elite",
        "qof_applies": et in {
            "continental_championships_elite",
            "continental_cup",
            "continental_u23_champs",
            "continental_junior_champs",
        },
        "in_ranking": base > 0,
    }


def points_at_rank(base: float, rank: int, top5_bonus_applies: bool = False) -> float:
    """Points for a single rank using 7.5% decay + optional top-5 bonus."""
    if rank < 1 or base <= 0:
        return 0.0
    pts = base * (WT_DECAY ** (rank - 1))
    if top5_bonus_applies and 1 <= rank <= 5:
        pts *= 1.0 + TOP5_BONUS[rank]
    return pts


def expected_points_from_probs(
    prob_win: float,
    prob_podium: float,
    prob_top5: float,
    prob_top10: float,
    prob_top20: float,
    base: float,
    field_size: int,
    top5_bonus_applies: bool = False,
) -> float:
    """
    E[points] from bucketed rank probabilities. Within each bucket we average
    the per-rank points, which is a close approximation given the smooth decay.

    Returns 0 if base <= 0 (event not in WT Ranking) or field is empty.
    """
    if base <= 0 or field_size < 1:
        return 0.0

    def avg_in_range(lo, hi):
        if lo > hi or lo > field_size:
            return 0.0
        hi = min(hi, field_size)
        n = hi - lo + 1
        return sum(points_at_rank(base, k, top5_bonus_applies) for k in range(lo, hi + 1)) / n

    # Bucket probabilities — clamp negatives from numerical noise
    buckets = [
        (max(0.0, prob_win),                 1,  1),
        (max(0.0, prob_podium - prob_win),   2,  3),
        (max(0.0, prob_top5 - prob_podium),  4,  5),
        (max(0.0, prob_top10 - prob_top5),   6, 10),
        (max(0.0, prob_top20 - prob_top10), 11, 20),
        (max(0.0, 1.0 - prob_top20),        21, field_size),
    ]

    e_pts = 0.0
    for p, lo, hi in buckets:
        if p > 0:
            e_pts += p * avg_in_range(lo, hi)
    return e_pts


def add_expected_points_to_display(
    sim_df: pd.DataFrame,
    display_df: pd.DataFrame,
    event_meta: dict | None,
) -> tuple[pd.DataFrame, dict]:
    """
    Augment `display_df` with two scoring columns:

      Pts @ Sim Rank — points the athlete would earn IF they finish at their
                       Sim Rank position. Deterministic, base × 0.925^(rank-1).
                       Placed right after "Sim Rank".

      Exp. WT Pts    — E[points] = integral over the rank distribution from
                       the Monte Carlo simulation, computed from bucketed
                       probabilities. Placed right after "E[Rank]".

    Both apply the 8% cut-off and (where relevant) the Continental Championships
    top-5 bonus. Both return None for events not in the WT Ranking system (T100).

    Row order in display_df is assumed to match sim_df (both sorted by
    mean_total_sec in format_simulation_output).
    """
    info = classify_event_for_wt_points(
        (event_meta or {}).get("event_name"),
        (event_meta or {}).get("cat_name"),
    )

    n_athletes = len(sim_df)
    if n_athletes == 0:
        return display_df, info

    new_df = display_df.copy()

    # Cut-off threshold from winner's sim median
    winner_p50 = sim_df["total_p50"].min() if "total_p50" in sim_df.columns else None

    deterministic_pts = []
    expected_pts = []
    # iterate sim_df positionally — display_df has been row-aligned in format_simulation_output
    sim_rank_series = (
        new_df["Sim Rank"].tolist() if "Sim Rank" in new_df.columns else [None] * len(new_df)
    )

    for i, (_, row) in enumerate(sim_df.iterrows()):
        # T100 / unknown: no WT points
        if not info["in_ranking"]:
            deterministic_pts.append(None)
            expected_pts.append(None)
            continue

        past_cutoff = (
            winner_p50 is not None
            and pd.notna(row.get("total_p50"))
            and row["total_p50"] > winner_p50 * CUT_OFF_FACTOR
        )

        if past_cutoff:
            deterministic_pts.append(0)
            expected_pts.append(0)
            continue

        # Deterministic: points at the athlete's Sim Rank
        rank = sim_rank_series[i] if i < len(sim_rank_series) else None
        if rank is None or pd.isna(rank):
            deterministic_pts.append(None)
        else:
            deterministic_pts.append(
                int(round(points_at_rank(info["base_points"], int(rank), info["top5_bonus_applies"])))
            )

        # Probabilistic: integral over the rank distribution
        e = expected_points_from_probs(
            prob_win=float(row.get("prob_win", 0) or 0),
            prob_podium=float(row.get("prob_podium", 0) or 0),
            prob_top5=float(row.get("prob_top5", 0) or 0),
            prob_top10=float(row.get("prob_top10", 0) or 0),
            prob_top20=float(row.get("prob_top20", row.get("prob_top10", 0)) or 0),
            base=info["base_points"],
            field_size=n_athletes,
            top5_bonus_applies=info["top5_bonus_applies"],
        )
        expected_pts.append(int(round(e)))

    new_df["Pts @ Sim Rank"] = deterministic_pts
    new_df["Exp. WT Pts"] = expected_pts

    # Reorder: Pts @ Sim Rank right after Sim Rank, Exp. WT Pts right after E[Rank]
    def _move_after(cols: list[str], target: str, after: str) -> list[str]:
        if target not in cols or after not in cols:
            return cols
        without = [c for c in cols if c != target]
        idx = without.index(after) + 1
        without.insert(idx, target)
        return without

    cols = list(new_df.columns)
    cols = _move_after(cols, "Pts @ Sim Rank", "Sim Rank")
    cols = _move_after(cols, "Exp. WT Pts", "E[Rank]")
    new_df = new_df[cols]

    return new_df, info
