"""Head-to-Head comparison service.

Pure data layer for the ``/compare`` page. Fetches per-athlete race data from
the triathlon-db, merges shared races in pandas, and produces dataclasses
that the route layer turns into HTMX-rendered cards.

All time fields stored in the triathlon-db are HH:MM:SS strings; we convert
to seconds on the fly. ``position_metrics`` already exposes integer-second
elapsed/gap values, which we prefer when available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import functools
import time as _time
from typing import Sequence

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.athlete_index import AthleteEntry, get_athlete_index
from app.utils.timefmt import distance_sort_key, slugify_athlete_name, time_to_seconds


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AthleteRef:
    athlete_id: int
    full_name: str
    slug: str
    country: str | None
    gender: str | None

    @classmethod
    def from_entry(cls, e: AthleteEntry) -> "AthleteRef":
        return cls(e.athlete_id, e.full_name, e.slug, e.country, e.gender)


@dataclass
class H2HRecord:
    matches: int
    wins_a: int
    wins_b: int
    ties: int
    last_met_event: str | None
    last_met_date: date | None
    last_met_event_id: int | None
    last_met_prog_id: int | None
    avg_gap_sec_a_minus_b: float | None  # negative = A faster on average


@dataclass
class HowTheyWin:
    swim_wins_a: int
    swim_wins_b: int
    swim_ties: int
    bike_wins_a: int
    bike_wins_b: int
    bike_ties: int
    run_wins_a: int
    run_wins_b: int
    run_ties: int
    transitions_wins_a: int  # T1 + T2 combined
    transitions_wins_b: int

    def total_swim(self) -> int:
        return self.swim_wins_a + self.swim_wins_b
    def total_bike(self) -> int:
        return self.bike_wins_a + self.bike_wins_b
    def total_run(self) -> int:
        return self.run_wins_a + self.run_wins_b


@dataclass
class SplitAverages:
    distance: str  # e.g. "Sprint", "Olympic"
    n_races_a: int
    n_races_b: int
    swim_a_sec: float | None
    swim_b_sec: float | None
    bike_a_sec: float | None
    bike_b_sec: float | None
    run_a_sec: float | None
    run_b_sec: float | None
    overall_a_sec: float | None
    overall_b_sec: float | None


@dataclass
class PackProfile:
    athlete_id: int
    races_with_pack_data: int
    # "Front group" = within FRONT_SWIM_GAP_SEC of leader at swim, FRONT_BIKE_GAP_SEC at bike.
    # More informative than pure pack_id==0 because the lead pack is often a
    # tiny breakaway (~7 athletes) and elite runners like Yee/Wilde never make it.
    front_after_swim_pct: float | None
    front_after_bike_pct: float | None
    lead_pack_after_swim_pct: float | None   # strict pack_id==0
    lead_pack_after_bike_pct: float | None
    avg_swim_gap_to_leader_sec: float | None
    avg_bike_gap_to_leader_sec: float | None


FRONT_SWIM_GAP_SEC = 20
FRONT_BIKE_GAP_SEC = 10


@dataclass
class TransitionH2H:
    t1_wins_a: int
    t1_wins_b: int
    t1_ties: int
    t2_wins_a: int
    t2_wins_b: int
    t2_ties: int
    avg_t1_a_sec: float | None
    avg_t1_b_sec: float | None
    avg_t2_a_sec: float | None
    avg_t2_b_sec: float | None
    avg_t1_gap_sec: float | None   # avg(t1_a - t1_b); negative = A faster
    avg_t2_gap_sec: float | None


@dataclass
class RaceLogRow:
    event_id: int
    prog_id: int
    event_name: str
    event_date: date | None
    event_venue: str | None
    distance: str | None
    prog_name: str | None
    pos_a: int | None
    pos_b: int | None
    finish_status_a: str | None
    finish_status_b: str | None
    # Per-segment times in seconds (None when split missing or athlete DNF'd)
    swim_a_sec: float | None
    swim_b_sec: float | None
    t1_a_sec: float | None
    t1_b_sec: float | None
    bike_a_sec: float | None
    bike_b_sec: float | None
    t2_a_sec: float | None
    t2_b_sec: float | None
    run_a_sec: float | None
    run_b_sec: float | None
    total_a_sec: float | None
    total_b_sec: float | None
    gap_sec: float | None  # total_a - total_b (None if either DNF)
    winner: str | None     # 'a' | 'b' | None (finisher beats DNF; both-DNF = None)
    # Pack membership (1-indexed for display; None when no wtcs_pack_membership row)
    swim_pack_a: int | None = None
    swim_pack_size_a: int | None = None
    bike_pack_a: int | None = None
    bike_pack_size_a: int | None = None
    swim_pack_b: int | None = None
    swim_pack_size_b: int | None = None
    bike_pack_b: int | None = None
    bike_pack_size_b: int | None = None


@dataclass
class SubsetRecord:
    """H2H record restricted to a subset of shared races (e.g. wetsuit-only)."""
    label: str
    matches: int
    wins_a: int
    wins_b: int


@dataclass
class ContextSection:
    title: str
    rows: list[SubsetRecord]
    note: str | None = None


@dataclass
class ContextH2H:
    """Environmental / contextual H2H breakdowns, organized into sections."""
    sections: list[ContextSection]


@dataclass
class CompareBundle:
    athlete_a: AthleteRef
    athlete_b: AthleteRef
    record: H2HRecord
    how_they_win: HowTheyWin
    split_averages: list[SplitAverages]
    pack_profile: list[PackProfile]   # always length 2: [a, b]
    transitions: TransitionH2H
    race_log: list[RaceLogRow]
    context: ContextH2H | None = None
    # Diagnostics
    has_any_shared: bool = True
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_athlete(token: str | int | None) -> AthleteRef | None:
    """Resolve a slug, raw name, or numeric ID to an AthleteRef via the index.

    Accepts a slug ("hayden-wilde"), a numeric ID, or a raw display name
    ("Hayden Wilde") — raw names are slugified before lookup. This means
    the form submit works whether the user picked from the dropdown
    (slug already populated) or just typed a name and hit Compare.
    """
    if token is None or token == "":
        return None
    idx = get_athlete_index()
    if isinstance(token, int):
        entry = idx.by_id(token)
        return AthleteRef.from_entry(entry) if entry else None
    s = str(token).strip()
    if not s:
        return None
    if s.isdigit():
        entry = idx.by_id(int(s))
        return AthleteRef.from_entry(entry) if entry else None
    entry = idx.by_slug(s)
    if entry:
        return AthleteRef.from_entry(entry)
    slug = slugify_athlete_name(s)
    if slug and slug != s:
        entry = idx.by_slug(slug)
        if entry:
            return AthleteRef.from_entry(entry)
    return None


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


_FINISHED_STATUSES = ("FINISH", "FINISHED", "OK", "OFFICIAL")


def fetch_athlete_races(athlete_id: int, *, engine: Engine) -> pd.DataFrame:
    """Return all race rows for an athlete joined with event + position_metrics.

    Includes Mixed Relay and Para programs; filter downstream as needed.
    Caller is responsible for finish_status filtering when computing stats.
    """
    sql = text("""
        SELECT
            rr.event_id, rr.prog_id, rr.athlete_id,
            rr.swimtime, rr.t1time, rr.biketime, rr.t2time, rr.runtime,
            rr.total_time, rr.position, rr.finish_status, rr.finish_position,
            e.event_name, e.event_venue, e.event_date, e.event_country,
            e.prog_name, e.prog_distance_category, e.is_para,
            e.swim_distance, e.bike_distance, e.run_distance,
            e.wetsuit, e.temperature_water, e.temperature_air, e.wbgt,
            pm.elapsedswim, pm.elapsedt1, pm.elapsedbike, pm.elapsedt2, pm.elapsedrun,
            pm.behindswim, pm.behindt1, pm.behindbike, pm.behindt2, pm.behindrun,
            pm.position_at_swim, pm.position_at_t1, pm.position_at_bike,
            pm.position_at_t2, pm.position_at_run,
            pm.swim_to_t1_pos_change, pm.t1_to_bike_pos_change,
            pm.bike_to_t2_pos_change, pm.t2_to_run_pos_change,
            pm.swimrank, pm.t1rank, pm.bikerank, pm.t2rank, pm.runrank,
            pm.n_finishers
        FROM race_results rr
        JOIN events e
          ON e.event_id = rr.event_id AND e.prog_id = rr.prog_id
        LEFT JOIN position_metrics pm
          ON pm.event_id = rr.event_id
         AND pm.prog_id  = rr.prog_id
         AND pm.athlete_id = rr.athlete_id
        WHERE rr.athlete_id = :aid
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"aid": int(athlete_id)})
    return df


def fetch_pack_membership(
    athlete_id: int,
    event_keys: Sequence[tuple[int, int]],
    *,
    engine: Engine,
) -> pd.DataFrame:
    """Return wtcs_pack_membership rows for an athlete restricted to specific
    (event_id, prog_id) pairs, at swim and bike checkpoints.
    """
    if not event_keys:
        return pd.DataFrame(columns=[
            "event_id", "prog_id", "athlete_id", "checkpoint",
            "pack_id", "pack_size", "gap_to_leader_sec", "pos_at_checkpoint",
        ])
    # psycopg expects tuple-of-tuples for IN VALUES.
    rows_clause = ", ".join(
        f"({int(e)}, {int(p)})" for e, p in event_keys
    )
    sql = text(f"""
        SELECT event_id, prog_id, athlete_id, checkpoint,
               pack_id, pack_size, gap_to_leader_sec, pos_at_checkpoint
        FROM wtcs_pack_membership
        WHERE athlete_id = :aid
          AND checkpoint IN ('swim', 'bike')
          AND (event_id, prog_id) IN ({rows_clause})
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"aid": int(athlete_id)})
    return df


# ---------------------------------------------------------------------------
# Merge + filter helpers
# ---------------------------------------------------------------------------


def _coerce_seconds_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``*_sec`` columns derived from the HH:MM:SS string columns."""
    for col in ("swimtime", "t1time", "biketime", "t2time", "runtime", "total_time"):
        if col in df.columns:
            df[f"{col}_sec"] = df[col].map(time_to_seconds)
    return df


def _finished_mask(df: pd.DataFrame, suffix: str) -> pd.Series:
    """True for rows where the athlete with given suffix officially finished."""
    status_col = f"finish_status{suffix}"
    if status_col not in df.columns:
        return pd.Series([True] * len(df), index=df.index)
    status = df[status_col].astype(str).str.upper().str.strip()
    return status.isin(_FINISHED_STATUSES)


# Race category classification (keep aligned with _ctx_section_distance keys
# and the UI checkbox values).
CATEGORY_KEYS: tuple[str, ...] = ("wtcs", "world_cup", "continental", "other")
CATEGORY_LABELS: dict[str, str] = {
    "wtcs": "WTCS",
    "world_cup": "World Cup",
    "continental": "Continental",
    "other": "Other",
}

DISTANCE_KEYS: tuple[str, ...] = (
    "super_sprint", "sprint", "standard", "middle_distance", "long_distance",
)


def classify_event_category(event_name: object, prog_name: object) -> str:
    """Bucket an event into one of {wtcs, world_cup, continental, other}.

    Uses the event name (and program name as a fallback) to detect the
    governing series. Olympics, T100, Super League, regional cups, etc.
    fall into 'other' — keep the top three buckets clean.
    """
    en = (str(event_name or "")).lower()
    pn = (str(prog_name or "")).lower()
    blob = f"{en} {pn}"
    if any(s in blob for s in (
        "championship series", "championship finals", "grand final", "wtcs",
    )):
        return "wtcs"
    if "world cup" in blob:
        return "world_cup"
    if any(s in blob for s in (
        "continental championship", "continental cup",
        "patco", "atu", "otu", "astc", "etu", "natu",
        "americas championship", "asian championship", "african championship",
        "european championship", "oceania championship", "european cup",
        "americas triathlon cup", "asia triathlon cup", "africa triathlon cup",
    )):
        return "continental"
    return "other"


def _shared_races_df(
    races_a: pd.DataFrame,
    races_b: pd.DataFrame,
    *,
    include_mixed_relay: bool = False,
    include_para: bool = False,
    years_back: int | None = None,
    categories: set[str] | None = None,
    distances: set[str] | None = None,
) -> pd.DataFrame:
    a = _coerce_seconds_columns(races_a.copy())
    b = _coerce_seconds_columns(races_b.copy())
    merged = a.merge(
        b,
        on=["event_id", "prog_id"],
        suffixes=("_a", "_b"),
        how="inner",
    )
    if merged.empty:
        return merged
    # Event metadata columns appear twice (once per athlete); collapse.
    for col in (
        "event_name", "event_venue", "event_date", "event_country",
        "prog_name", "prog_distance_category", "is_para",
        "swim_distance", "bike_distance", "run_distance",
        "wetsuit", "temperature_water", "temperature_air", "wbgt",
    ):
        a_col, b_col = f"{col}_a", f"{col}_b"
        if a_col in merged.columns and b_col in merged.columns:
            collapsed = merged[a_col].where(merged[a_col].notna(), merged[b_col])
            merged[col] = collapsed
            merged = merged.drop(columns=[a_col, b_col])

    if not include_mixed_relay and "prog_name" in merged.columns:
        merged = merged[merged["prog_name"].astype(str).str.strip().str.lower() != "mixed relay"]
    if not include_para and "is_para" in merged.columns:
        is_para = merged["is_para"].map(lambda v: bool(v) if pd.notna(v) else False)
        merged = merged[~is_para]
    if years_back is not None and "event_date" in merged.columns:
        cutoff = date.today().replace(year=date.today().year - int(years_back))
        ed = pd.to_datetime(merged["event_date"], errors="coerce")
        merged = merged[ed.dt.date >= cutoff]
    if categories:
        cats_norm = {c.strip().lower() for c in categories if c}
        if cats_norm:
            cat_series = merged.apply(
                lambda r: classify_event_category(r.get("event_name"), r.get("prog_name")),
                axis=1,
            )
            merged = merged[cat_series.isin(cats_norm)]
    if distances:
        dists_norm = {d.strip().lower() for d in distances if d}
        if dists_norm and "prog_distance_category" in merged.columns:
            merged = merged[merged["prog_distance_category"].astype(str).str.lower().isin(dists_norm)]
    return merged.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def _build_record(shared: pd.DataFrame) -> H2HRecord:
    """Build the H2H record across all shared races.

    Counts a race when BOTH athletes appear (even if one DNF'd) — matches
    Pro Tri News behavior. A finisher beats a non-finisher. Both-DNF is a
    counted match but no winner. Avg gap is restricted to races where
    both finished with valid total times.
    """
    if shared.empty:
        return H2HRecord(0, 0, 0, 0, None, None, None, None, None)

    df = shared.copy()
    a_fin = _finished_mask(df, "_a").values
    b_fin = _finished_mask(df, "_b").values
    a_total = df["total_time_sec_a"].values if "total_time_sec_a" in df.columns else [None] * len(df)
    b_total = df["total_time_sec_b"].values if "total_time_sec_b" in df.columns else [None] * len(df)

    wins_a = 0
    wins_b = 0
    ties = 0
    for i in range(len(df)):
        af, bf = bool(a_fin[i]), bool(b_fin[i])
        ta, tb = a_total[i], b_total[i]
        ta_ok = ta is not None and not pd.isna(ta)
        tb_ok = tb is not None and not pd.isna(tb)
        if af and bf and ta_ok and tb_ok:
            if ta < tb:
                wins_a += 1
            elif tb < ta:
                wins_b += 1
            else:
                ties += 1
        elif af and not bf:
            wins_a += 1
        elif bf and not af:
            wins_b += 1
        # else: both DNF — counted in matches, no winner

    matches = len(df)

    # Avg gap is for FINISHED-BOTH races only (per user feedback)
    finished_both = df[
        _finished_mask(df, "_a")
        & _finished_mask(df, "_b")
        & df["total_time_sec_a"].notna()
        & df["total_time_sec_b"].notna()
    ]
    avg_gap = None
    if not finished_both.empty:
        avg_gap = float((finished_both["total_time_sec_a"] - finished_both["total_time_sec_b"]).mean())

    # Latest shared race (any finish status)
    last_idx = None
    if "event_date" in df.columns and df["event_date"].notna().any():
        last_idx = df["event_date"].idxmax()
    else:
        last_idx = df.index[-1]
    last_row = df.loc[last_idx]
    last_event = str(last_row.get("event_name") or "") or None
    last_date = last_row.get("event_date")
    if isinstance(last_date, pd.Timestamp):
        last_date = last_date.date()
    elif isinstance(last_date, datetime):
        last_date = last_date.date()
    last_eid = int(last_row["event_id"]) if not pd.isna(last_row.get("event_id")) else None
    last_pid = int(last_row["prog_id"]) if not pd.isna(last_row.get("prog_id")) else None

    return H2HRecord(
        matches=matches,
        wins_a=wins_a,
        wins_b=wins_b,
        ties=ties,
        last_met_event=last_event,
        last_met_date=last_date if isinstance(last_date, date) else None,
        last_met_event_id=last_eid,
        last_met_prog_id=last_pid,
        avg_gap_sec_a_minus_b=avg_gap,
    )


def _segment_wins(df: pd.DataFrame, a_col: str, b_col: str) -> tuple[int, int, int]:
    valid = df[[a_col, b_col]].dropna()
    if valid.empty:
        return 0, 0, 0
    wins_a = int((valid[a_col] < valid[b_col]).sum())
    wins_b = int((valid[b_col] < valid[a_col]).sum())
    ties = int((valid[a_col] == valid[b_col]).sum())
    return wins_a, wins_b, ties


def _build_how_they_win(shared: pd.DataFrame) -> HowTheyWin:
    if shared.empty:
        return HowTheyWin(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    df = shared[_finished_mask(shared, "_a") & _finished_mask(shared, "_b")].copy()
    if df.empty:
        return HowTheyWin(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    sw_a, sw_b, sw_t = _segment_wins(df, "swimtime_sec_a", "swimtime_sec_b")
    bk_a, bk_b, bk_t = _segment_wins(df, "biketime_sec_a", "biketime_sec_b")
    rn_a, rn_b, rn_t = _segment_wins(df, "runtime_sec_a", "runtime_sec_b")
    t1_a, t1_b, _ = _segment_wins(df, "t1time_sec_a", "t1time_sec_b")
    t2_a, t2_b, _ = _segment_wins(df, "t2time_sec_a", "t2time_sec_b")
    return HowTheyWin(
        swim_wins_a=sw_a, swim_wins_b=sw_b, swim_ties=sw_t,
        bike_wins_a=bk_a, bike_wins_b=bk_b, bike_ties=bk_t,
        run_wins_a=rn_a,  run_wins_b=rn_b,  run_ties=rn_t,
        transitions_wins_a=t1_a + t2_a,
        transitions_wins_b=t1_b + t2_b,
    )


def _iqr_mean(values: pd.Series) -> float | None:
    s = values.dropna()
    if s.empty:
        return None
    if len(s) < 4:
        return float(s.mean())
    q1 = s.quantile(0.25)
    q3 = s.quantile(0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    trimmed = s[(s >= lo) & (s <= hi)]
    if trimmed.empty:
        return float(s.mean())
    return float(trimmed.mean())


def _build_split_averages(
    races_a: pd.DataFrame,
    races_b: pd.DataFrame,
    *,
    years_back: int = 4,
    categories: set[str] | None = None,
    distances: set[str] | None = None,
) -> list[SplitAverages]:
    """Per-distance averages computed independently for each athlete.

    Note: NOT restricted to shared races — uses each athlete's individual
    record over the last ``years_back`` years, with IQR outlier removal
    per (athlete, distance, segment). This matches the Pro Tri News card.
    """
    today = date.today()
    cutoff = today.replace(year=today.year - years_back)
    cats_norm = {c.strip().lower() for c in categories if c} if categories else None
    dists_norm = {d.strip().lower() for d in distances if d} if distances else None

    def _prep(df: pd.DataFrame) -> pd.DataFrame:
        x = _coerce_seconds_columns(df.copy())
        if "event_date" in x.columns:
            ed = pd.to_datetime(x["event_date"], errors="coerce")
            x = x[ed.dt.date >= cutoff]
        # Finished only, exclude Mixed Relay and Para from averages
        status = x["finish_status"].astype(str).str.upper().str.strip()
        x = x[status.isin(_FINISHED_STATUSES)]
        x = x[x["prog_name"].astype(str).str.strip().str.lower() != "mixed relay"]
        if "is_para" in x.columns:
            is_para = x["is_para"].map(lambda v: bool(v) if pd.notna(v) else False)
            x = x[~is_para]
        if cats_norm:
            cat_series = x.apply(
                lambda r: classify_event_category(r.get("event_name"), r.get("prog_name")),
                axis=1,
            )
            x = x[cat_series.isin(cats_norm)]
        if dists_norm and "prog_distance_category" in x.columns:
            x = x[x["prog_distance_category"].astype(str).str.lower().isin(dists_norm)]
        return x

    a = _prep(races_a)
    b = _prep(races_b)

    distances: set[str] = set()
    for x in (a, b):
        if "prog_distance_category" in x.columns:
            distances.update(
                str(v) for v in x["prog_distance_category"].dropna().unique()
                if str(v).strip()
            )

    out: list[SplitAverages] = []
    for dist in sorted(distances, key=distance_sort_key):
        a_dist = a[a["prog_distance_category"] == dist]
        b_dist = b[b["prog_distance_category"] == dist]
        if a_dist.empty and b_dist.empty:
            continue
        out.append(SplitAverages(
            distance=dist,
            n_races_a=len(a_dist),
            n_races_b=len(b_dist),
            swim_a_sec=_iqr_mean(a_dist.get("swimtime_sec", pd.Series(dtype=float))),
            swim_b_sec=_iqr_mean(b_dist.get("swimtime_sec", pd.Series(dtype=float))),
            bike_a_sec=_iqr_mean(a_dist.get("biketime_sec", pd.Series(dtype=float))),
            bike_b_sec=_iqr_mean(b_dist.get("biketime_sec", pd.Series(dtype=float))),
            run_a_sec=_iqr_mean(a_dist.get("runtime_sec", pd.Series(dtype=float))),
            run_b_sec=_iqr_mean(b_dist.get("runtime_sec", pd.Series(dtype=float))),
            overall_a_sec=_iqr_mean(a_dist.get("total_time_sec", pd.Series(dtype=float))),
            overall_b_sec=_iqr_mean(b_dist.get("total_time_sec", pd.Series(dtype=float))),
        ))
    return out


def _empty_pack_profile(athlete_id: int) -> PackProfile:
    return PackProfile(athlete_id, 0, None, None, None, None, None, None)


def _build_pack_profile(athlete_id: int, pack_df: pd.DataFrame) -> PackProfile:
    if pack_df.empty:
        return _empty_pack_profile(athlete_id)
    df = pack_df[pack_df["athlete_id"] == athlete_id]
    if df.empty:
        return _empty_pack_profile(athlete_id)

    swim = df[df["checkpoint"] == "swim"]
    bike = df[df["checkpoint"] == "bike"]
    n = max(len(swim), len(bike))

    def _lead_pct(x: pd.DataFrame) -> float | None:
        if x.empty:
            return None
        return float((x["pack_id"] == 0).sum()) / float(len(x))

    def _front_pct(x: pd.DataFrame, threshold: int) -> float | None:
        if x.empty:
            return None
        gaps = x["gap_to_leader_sec"].dropna()
        if gaps.empty:
            return None
        return float((gaps <= threshold).sum()) / float(len(gaps))

    def _avg(s: pd.Series) -> float | None:
        s = s.dropna()
        return float(s.mean()) if not s.empty else None

    return PackProfile(
        athlete_id=athlete_id,
        races_with_pack_data=n,
        front_after_swim_pct=_front_pct(swim, FRONT_SWIM_GAP_SEC),
        front_after_bike_pct=_front_pct(bike, FRONT_BIKE_GAP_SEC),
        lead_pack_after_swim_pct=_lead_pct(swim),
        lead_pack_after_bike_pct=_lead_pct(bike),
        avg_swim_gap_to_leader_sec=_avg(swim["gap_to_leader_sec"]) if not swim.empty else None,
        avg_bike_gap_to_leader_sec=_avg(bike["gap_to_leader_sec"]) if not bike.empty else None,
    )


def _build_transitions(shared: pd.DataFrame) -> TransitionH2H:
    if shared.empty:
        return TransitionH2H(0, 0, 0, 0, 0, 0, None, None, None, None, None, None)
    df = shared[_finished_mask(shared, "_a") & _finished_mask(shared, "_b")].copy()
    if df.empty:
        return TransitionH2H(0, 0, 0, 0, 0, 0, None, None, None, None, None, None)

    t1_a, t1_b, t1_t = _segment_wins(df, "t1time_sec_a", "t1time_sec_b")
    t2_a, t2_b, t2_t = _segment_wins(df, "t2time_sec_a", "t2time_sec_b")

    def _mean(col: str) -> float | None:
        s = df[col].dropna() if col in df.columns else pd.Series(dtype=float)
        return float(s.mean()) if not s.empty else None

    avg_t1_a = _mean("t1time_sec_a")
    avg_t1_b = _mean("t1time_sec_b")
    avg_t2_a = _mean("t2time_sec_a")
    avg_t2_b = _mean("t2time_sec_b")
    gap_t1 = None
    if "t1time_sec_a" in df.columns and "t1time_sec_b" in df.columns:
        d = (df["t1time_sec_a"] - df["t1time_sec_b"]).dropna()
        gap_t1 = float(d.mean()) if not d.empty else None
    gap_t2 = None
    if "t2time_sec_a" in df.columns and "t2time_sec_b" in df.columns:
        d = (df["t2time_sec_a"] - df["t2time_sec_b"]).dropna()
        gap_t2 = float(d.mean()) if not d.empty else None

    return TransitionH2H(
        t1_wins_a=t1_a, t1_wins_b=t1_b, t1_ties=t1_t,
        t2_wins_a=t2_a, t2_wins_b=t2_b, t2_ties=t2_t,
        avg_t1_a_sec=avg_t1_a, avg_t1_b_sec=avg_t1_b,
        avg_t2_a_sec=avg_t2_a, avg_t2_b_sec=avg_t2_b,
        avg_t1_gap_sec=gap_t1, avg_t2_gap_sec=gap_t2,
    )


_WETSUIT_LEGAL = {"mandatory", "allowed", "true", "yes", "y", "1"}
_WETSUIT_FORBIDDEN = {"forbidden", "false", "no", "n", "0"}

# WBGT threshold for World Triathlon hot-weather protocol
HOT_WBGT_C = 28.0
# Wind threshold (km/h) where bike tactics shift noticeably
WINDY_KMH = 20.0

# Continent bucketing for the common triathlon-hosting countries.
# Anything not listed falls into "Other".
_COUNTRY_TO_CONTINENT: dict[str, str] = {
    # Europe
    "great britain": "Europe", "united kingdom": "Europe", "france": "Europe",
    "spain": "Europe", "germany": "Europe", "italy": "Europe",
    "netherlands": "Europe", "portugal": "Europe", "switzerland": "Europe",
    "austria": "Europe", "hungary": "Europe", "czech republic": "Europe",
    "czechia": "Europe", "slovakia": "Europe", "poland": "Europe",
    "belgium": "Europe", "sweden": "Europe", "norway": "Europe",
    "denmark": "Europe", "finland": "Europe", "estonia": "Europe",
    "ireland": "Europe", "greece": "Europe", "croatia": "Europe",
    "slovenia": "Europe", "russia": "Europe", "türkiye": "Europe",
    "turkey": "Europe", "romania": "Europe", "bulgaria": "Europe",
    "ukraine": "Europe", "luxembourg": "Europe", "monaco": "Europe",
    "andorra": "Europe", "lithuania": "Europe", "latvia": "Europe",
    "iceland": "Europe", "serbia": "Europe",
    # Americas
    "united states": "Americas", "usa": "Americas", "canada": "Americas",
    "mexico": "Americas", "brazil": "Americas", "argentina": "Americas",
    "chile": "Americas", "colombia": "Americas", "peru": "Americas",
    "venezuela": "Americas", "bermuda": "Americas", "uruguay": "Americas",
    "ecuador": "Americas", "bolivia": "Americas", "paraguay": "Americas",
    "panama": "Americas", "costa rica": "Americas", "guatemala": "Americas",
    "barbados": "Americas", "trinidad and tobago": "Americas",
    "dominican republic": "Americas", "el salvador": "Americas",
    "honduras": "Americas", "nicaragua": "Americas", "jamaica": "Americas",
    "puerto rico": "Americas",
    # Asia-Oceania
    "japan": "Asia-Oceania", "china": "Asia-Oceania", "south korea": "Asia-Oceania",
    "korea": "Asia-Oceania", "republic of korea": "Asia-Oceania",
    "australia": "Asia-Oceania", "new zealand": "Asia-Oceania",
    "india": "Asia-Oceania", "thailand": "Asia-Oceania",
    "singapore": "Asia-Oceania", "hong kong": "Asia-Oceania",
    "indonesia": "Asia-Oceania", "philippines": "Asia-Oceania",
    "vietnam": "Asia-Oceania", "taiwan": "Asia-Oceania", "malaysia": "Asia-Oceania",
    "kazakhstan": "Asia-Oceania", "uzbekistan": "Asia-Oceania",
    "saudi arabia": "Asia-Oceania", "united arab emirates": "Asia-Oceania",
    "qatar": "Asia-Oceania", "bahrain": "Asia-Oceania", "oman": "Asia-Oceania",
    "jordan": "Asia-Oceania", "lebanon": "Asia-Oceania", "israel": "Asia-Oceania",
    "fiji": "Asia-Oceania", "samoa": "Asia-Oceania", "tonga": "Asia-Oceania",
    "papua new guinea": "Asia-Oceania",
    # Africa
    "south africa": "Africa", "egypt": "Africa", "morocco": "Africa",
    "tunisia": "Africa", "kenya": "Africa", "mauritius": "Africa",
    "algeria": "Africa", "namibia": "Africa", "rwanda": "Africa",
    "uganda": "Africa", "zimbabwe": "Africa", "senegal": "Africa",
    "nigeria": "Africa", "ghana": "Africa", "ethiopia": "Africa",
    "botswana": "Africa", "zambia": "Africa",
}


def _country_to_continent(country: object) -> str:
    if not country:
        return "Other"
    key = str(country).strip().lower()
    return _COUNTRY_TO_CONTINENT.get(key, "Other")


def _classify_wetsuit(value: object) -> bool | None:
    """Classify a row's wetsuit column. True=wetsuit-legal, False=non-wetsuit, None=unknown."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v in _WETSUIT_LEGAL:
        return True
    if v in _WETSUIT_FORBIDDEN:
        return False
    return None


def _to_float(v: object) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(str(v).strip())
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None


def _subset_record(df: pd.DataFrame, label: str) -> SubsetRecord:
    """Count wins on a subset using the same rules as _build_record."""
    if df.empty:
        return SubsetRecord(label=label, matches=0, wins_a=0, wins_b=0)
    a_fin = _finished_mask(df, "_a").values
    b_fin = _finished_mask(df, "_b").values
    a_total = df["total_time_sec_a"].values if "total_time_sec_a" in df.columns else [None] * len(df)
    b_total = df["total_time_sec_b"].values if "total_time_sec_b" in df.columns else [None] * len(df)
    wa, wb = 0, 0
    for i in range(len(df)):
        af, bf = bool(a_fin[i]), bool(b_fin[i])
        ta, tb = a_total[i], b_total[i]
        ta_ok = ta is not None and not pd.isna(ta)
        tb_ok = tb is not None and not pd.isna(tb)
        if af and bf and ta_ok and tb_ok:
            if ta < tb:
                wa += 1
            elif tb < ta:
                wb += 1
        elif af and not bf:
            wa += 1
        elif bf and not af:
            wb += 1
    return SubsetRecord(label=label, matches=len(df), wins_a=wa, wins_b=wb)


def _ctx_section_distance(shared: pd.DataFrame) -> ContextSection | None:
    if "prog_distance_category" not in shared.columns:
        return None
    distances = [str(v) for v in shared["prog_distance_category"].dropna().unique() if str(v).strip()]
    rows = []
    from app.utils.timefmt import distance_label, distance_sort_key
    for d in sorted(distances, key=distance_sort_key):
        subset = shared[shared["prog_distance_category"] == d]
        rec = _subset_record(subset, distance_label(d))
        if rec.matches:
            rows.append(rec)
    if not rows:
        return None
    return ContextSection(title="By Distance", rows=rows)


def _ctx_section_wetsuit(shared: pd.DataFrame) -> ContextSection | None:
    if "wetsuit" not in shared.columns:
        return None
    classified = shared["wetsuit"].map(_classify_wetsuit)
    wet = _subset_record(shared[classified == True], "Wetsuit swims")
    non = _subset_record(shared[classified == False], "Non-wetsuit swims")
    if wet.matches + non.matches == 0:
        return None
    rows = [r for r in (wet, non) if r.matches]
    unknown = int((classified.isna()).sum())
    note = None
    if unknown:
        note = f"{unknown} race{'s' if unknown != 1 else ''} with unknown wetsuit status (older events missing the flag)."
    return ContextSection(title="By Wetsuit", rows=rows, note=note)


def _ctx_section_conditions(shared: pd.DataFrame) -> ContextSection | None:
    """Hot/cool (WBGT) and windy/calm (wind_speed_kmh) breakdowns."""
    rows: list[SubsetRecord] = []
    if "wbgt" in shared.columns:
        wbgt = shared["wbgt"].map(_to_float)
        hot = _subset_record(shared[wbgt >= HOT_WBGT_C], f"Hot (WBGT ≥ {HOT_WBGT_C:.0f}°C)")
        cool = _subset_record(shared[(wbgt.notna()) & (wbgt < HOT_WBGT_C)], f"Cool (WBGT < {HOT_WBGT_C:.0f}°C)")
        if hot.matches:
            rows.append(hot)
        if cool.matches:
            rows.append(cool)
    if "wind_speed_kmh" in shared.columns:
        wind = shared["wind_speed_kmh"].map(_to_float)
        windy = _subset_record(shared[wind >= WINDY_KMH], f"Windy (≥ {WINDY_KMH:.0f} km/h)")
        calm = _subset_record(shared[(wind.notna()) & (wind < WINDY_KMH)], f"Calm (< {WINDY_KMH:.0f} km/h)")
        if windy.matches:
            rows.append(windy)
        if calm.matches:
            rows.append(calm)
    if not rows:
        return None
    return ContextSection(title="By Conditions", rows=rows)


def _ctx_section_continent(shared: pd.DataFrame) -> ContextSection | None:
    if "event_country" not in shared.columns:
        return None
    continents = shared["event_country"].map(_country_to_continent)
    seen = []
    rows = []
    # Stable display order
    for cont in ["Europe", "Americas", "Asia-Oceania", "Africa", "Other"]:
        subset = shared[continents == cont]
        if subset.empty:
            continue
        rec = _subset_record(subset, cont)
        if rec.matches:
            rows.append(rec)
            seen.append(cont)
    if not rows:
        return None
    return ContextSection(title="By Continent", rows=rows)


def _build_context(shared: pd.DataFrame) -> ContextH2H:
    if shared.empty:
        return ContextH2H(sections=[])
    sections: list[ContextSection] = []
    for builder in (
        _ctx_section_distance,
        _ctx_section_wetsuit,
        _ctx_section_conditions,
        _ctx_section_continent,
    ):
        s = builder(shared)
        if s is not None:
            sections.append(s)
    return ContextH2H(sections=sections)


def _build_race_log(shared: pd.DataFrame) -> list[RaceLogRow]:
    if shared.empty:
        return []
    df = shared.copy()
    if "event_date" in df.columns:
        df = df.sort_values("event_date", ascending=False, na_position="last")

    def _f(v):
        return float(v) if v is not None and not pd.isna(v) else None

    rows: list[RaceLogRow] = []
    for _, r in df.iterrows():
        a_fin = str(r.get("finish_status_a") or "").upper().strip()
        b_fin = str(r.get("finish_status_b") or "").upper().strip()
        ta = _f(r.get("total_time_sec_a"))
        tb = _f(r.get("total_time_sec_b"))
        a_finished = a_fin in _FINISHED_STATUSES
        b_finished = b_fin in _FINISHED_STATUSES

        # Gap and winner: gap only if both have valid total times AND both finished
        gap = None
        winner: str | None = None
        if a_finished and b_finished and ta is not None and tb is not None:
            gap = ta - tb
            if ta < tb:
                winner = "a"
            elif tb < ta:
                winner = "b"
        elif a_finished and not b_finished:
            winner = "a"
        elif b_finished and not a_finished:
            winner = "b"

        pos_a = r.get("finish_position_a")
        pos_b = r.get("finish_position_b")
        ed = r.get("event_date")
        if isinstance(ed, pd.Timestamp):
            ed = ed.date()
        elif isinstance(ed, datetime):
            ed = ed.date()
        rows.append(RaceLogRow(
            event_id=int(r["event_id"]),
            prog_id=int(r["prog_id"]),
            event_name=str(r.get("event_name") or ""),
            event_date=ed if isinstance(ed, date) else None,
            event_venue=str(r.get("event_venue") or "") or None,
            distance=str(r.get("prog_distance_category") or "") or None,
            prog_name=str(r.get("prog_name") or "") or None,
            pos_a=int(pos_a) if pd.notna(pos_a) else None,
            pos_b=int(pos_b) if pd.notna(pos_b) else None,
            finish_status_a=a_fin or None,
            finish_status_b=b_fin or None,
            swim_a_sec=_f(r.get("swimtime_sec_a")),
            swim_b_sec=_f(r.get("swimtime_sec_b")),
            t1_a_sec=_f(r.get("t1time_sec_a")),
            t1_b_sec=_f(r.get("t1time_sec_b")),
            bike_a_sec=_f(r.get("biketime_sec_a")),
            bike_b_sec=_f(r.get("biketime_sec_b")),
            t2_a_sec=_f(r.get("t2time_sec_a")),
            t2_b_sec=_f(r.get("t2time_sec_b")),
            run_a_sec=_f(r.get("runtime_sec_a")),
            run_b_sec=_f(r.get("runtime_sec_b")),
            total_a_sec=ta,
            total_b_sec=tb,
            gap_sec=gap,
            winner=winner,
        ))
    return rows


def _enrich_race_log_with_pack(
    rows: list[RaceLogRow],
    pack_df: pd.DataFrame,
    a_id: int,
    b_id: int,
) -> list[RaceLogRow]:
    """Attach swim/bike pack membership to each race row.

    Pack IDs in wtcs_pack_membership are 0-indexed (0 = lead pack); we
    store them 1-indexed for display (Pack 1 = lead pack).
    """
    if pack_df.empty or not rows:
        return rows
    # Build lookup keyed on (event_id, prog_id, athlete_id, checkpoint)
    lookup: dict[tuple[int, int, int, str], tuple[int, int]] = {}
    for _, r in pack_df.iterrows():
        key = (int(r["event_id"]), int(r["prog_id"]), int(r["athlete_id"]), str(r["checkpoint"]))
        lookup[key] = (int(r["pack_id"]) + 1, int(r["pack_size"]))   # +1 for 1-indexed display
    for row in rows:
        for athlete_id, suffix in ((a_id, "a"), (b_id, "b")):
            sw = lookup.get((row.event_id, row.prog_id, int(athlete_id), "swim"))
            bk = lookup.get((row.event_id, row.prog_id, int(athlete_id), "bike"))
            if sw:
                setattr(row, f"swim_pack_{suffix}", sw[0])
                setattr(row, f"swim_pack_size_{suffix}", sw[1])
            if bk:
                setattr(row, f"bike_pack_{suffix}", bk[0])
                setattr(row, f"bike_pack_size_{suffix}", bk[1])
    return rows


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


_BundleCacheKey = tuple[int, int, int | None, frozenset, frozenset]
_BUNDLE_CACHE: dict[_BundleCacheKey, tuple[float, CompareBundle]] = {}
_CACHE_TTL_SEC = 3600
_CACHE_MAX = 256


def _cache_get(key: _BundleCacheKey) -> CompareBundle | None:
    entry = _BUNDLE_CACHE.get(key)
    if not entry:
        return None
    ts, bundle = entry
    if (_time.time() - ts) > _CACHE_TTL_SEC:
        _BUNDLE_CACHE.pop(key, None)
        return None
    return bundle


def _cache_put(key: _BundleCacheKey, bundle: CompareBundle) -> None:
    if len(_BUNDLE_CACHE) >= _CACHE_MAX:
        # Drop oldest
        oldest_key = min(_BUNDLE_CACHE, key=lambda k: _BUNDLE_CACHE[k][0])
        _BUNDLE_CACHE.pop(oldest_key, None)
    _BUNDLE_CACHE[key] = (_time.time(), bundle)


def build_compare_bundle(
    a_id: int,
    b_id: int,
    *,
    engine: Engine,
    use_cache: bool = True,
    years_back: int | None = None,
    categories: set[str] | None = None,
    distances: set[str] | None = None,
) -> CompareBundle:
    """Top-level: fetch + assemble all cards for the (a, b) athlete pair.

    Filter semantics:
      - ``years_back``: limit shared races + split averages to last N years
      - ``categories``: restrict to race categories (wtcs, world_cup,
        continental, other). None or empty => no restriction.
      - ``distances``: restrict to distance categories (super_sprint,
        sprint, standard, middle_distance, long_distance). None or empty
        => no restriction.
    """
    if a_id == b_id:
        raise ValueError("Athletes must be different")
    sorted_ids = tuple(sorted((int(a_id), int(b_id))))
    yb = int(years_back) if years_back is not None else None
    cats_set = {c.lower() for c in categories} if categories else set()
    dists_set = {d.lower() for d in distances if d} if distances else set()
    key: _BundleCacheKey = (sorted_ids[0], sorted_ids[1], yb, frozenset(cats_set), frozenset(dists_set))
    cached = _cache_get(key) if use_cache else None
    if cached:
        # Cache stores in sorted order. If caller asked in reverse, swap.
        if cached.athlete_a.athlete_id == int(a_id):
            return cached
        else:
            return _swap_bundle(cached)

    idx = get_athlete_index()
    a_entry = idx.by_id(int(a_id))
    b_entry = idx.by_id(int(b_id))
    if a_entry is None or b_entry is None:
        raise ValueError(f"Unknown athlete(s): a={a_id} b={b_id}")

    races_a = fetch_athlete_races(a_id, engine=engine)
    races_b = fetch_athlete_races(b_id, engine=engine)
    shared = _shared_races_df(
        races_a, races_b,
        years_back=yb,
        categories=cats_set or None,
        distances=dists_set or None,
    )
    split_years = yb if yb is not None else 4

    if shared.empty:
        bundle = CompareBundle(
            athlete_a=AthleteRef.from_entry(a_entry),
            athlete_b=AthleteRef.from_entry(b_entry),
            record=H2HRecord(0, 0, 0, 0, None, None, None, None, None),
            how_they_win=HowTheyWin(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            split_averages=_build_split_averages(
                races_a, races_b,
                years_back=split_years,
                categories=cats_set or None,
                distances=dists_set or None,
            ),
            pack_profile=[_empty_pack_profile(int(a_id)), _empty_pack_profile(int(b_id))],
            transitions=TransitionH2H(0, 0, 0, 0, 0, 0, None, None, None, None, None, None),
            race_log=[],
            context=ContextH2H(sections=[]),
            has_any_shared=False,
        )
        if use_cache:
            _cache_put(key, _swap_to_sorted(bundle, sorted_ids))
        return bundle

    keys = list({(int(e), int(p)) for e, p in zip(shared["event_id"], shared["prog_id"])})
    pack_a = fetch_pack_membership(int(a_id), keys, engine=engine)
    pack_b = fetch_pack_membership(int(b_id), keys, engine=engine)
    pack_all = pd.concat([pack_a, pack_b], ignore_index=True) if not (pack_a.empty and pack_b.empty) else pd.DataFrame()

    bundle = CompareBundle(
        athlete_a=AthleteRef.from_entry(a_entry),
        athlete_b=AthleteRef.from_entry(b_entry),
        record=_build_record(shared),
        how_they_win=_build_how_they_win(shared),
        split_averages=_build_split_averages(
            races_a, races_b,
            years_back=split_years,
            categories=cats_set or None,
            distances=dists_set or None,
        ),
        pack_profile=[
            _build_pack_profile(int(a_id), pack_all),
            _build_pack_profile(int(b_id), pack_all),
        ],
        transitions=_build_transitions(shared),
        race_log=_enrich_race_log_with_pack(_build_race_log(shared), pack_all, int(a_id), int(b_id)),
        context=_build_context(shared),
        has_any_shared=True,
    )
    if use_cache:
        _cache_put(key, _swap_to_sorted(bundle, sorted_ids))
    return bundle


def _swap_bundle(bundle: CompareBundle) -> CompareBundle:
    """Return a copy of bundle with athlete A and B (and all per-athlete fields) swapped."""
    htw = bundle.how_they_win
    htw_swapped = HowTheyWin(
        swim_wins_a=htw.swim_wins_b, swim_wins_b=htw.swim_wins_a, swim_ties=htw.swim_ties,
        bike_wins_a=htw.bike_wins_b, bike_wins_b=htw.bike_wins_a, bike_ties=htw.bike_ties,
        run_wins_a=htw.run_wins_b,   run_wins_b=htw.run_wins_a,   run_ties=htw.run_ties,
        transitions_wins_a=htw.transitions_wins_b, transitions_wins_b=htw.transitions_wins_a,
    )
    rec = bundle.record
    rec_swapped = H2HRecord(
        matches=rec.matches, wins_a=rec.wins_b, wins_b=rec.wins_a, ties=rec.ties,
        last_met_event=rec.last_met_event, last_met_date=rec.last_met_date,
        last_met_event_id=rec.last_met_event_id, last_met_prog_id=rec.last_met_prog_id,
        avg_gap_sec_a_minus_b=(-rec.avg_gap_sec_a_minus_b if rec.avg_gap_sec_a_minus_b is not None else None),
    )
    sa_swapped = [
        SplitAverages(
            distance=s.distance,
            n_races_a=s.n_races_b, n_races_b=s.n_races_a,
            swim_a_sec=s.swim_b_sec, swim_b_sec=s.swim_a_sec,
            bike_a_sec=s.bike_b_sec, bike_b_sec=s.bike_a_sec,
            run_a_sec=s.run_b_sec,   run_b_sec=s.run_a_sec,
            overall_a_sec=s.overall_b_sec, overall_b_sec=s.overall_a_sec,
        )
        for s in bundle.split_averages
    ]
    pp = bundle.pack_profile
    pp_swapped = [pp[1], pp[0]] if len(pp) == 2 else pp
    tr = bundle.transitions
    tr_swapped = TransitionH2H(
        t1_wins_a=tr.t1_wins_b, t1_wins_b=tr.t1_wins_a, t1_ties=tr.t1_ties,
        t2_wins_a=tr.t2_wins_b, t2_wins_b=tr.t2_wins_a, t2_ties=tr.t2_ties,
        avg_t1_a_sec=tr.avg_t1_b_sec, avg_t1_b_sec=tr.avg_t1_a_sec,
        avg_t2_a_sec=tr.avg_t2_b_sec, avg_t2_b_sec=tr.avg_t2_a_sec,
        avg_t1_gap_sec=(-tr.avg_t1_gap_sec if tr.avg_t1_gap_sec is not None else None),
        avg_t2_gap_sec=(-tr.avg_t2_gap_sec if tr.avg_t2_gap_sec is not None else None),
    )
    log_swapped = [
        RaceLogRow(
            event_id=r.event_id, prog_id=r.prog_id, event_name=r.event_name,
            event_date=r.event_date, event_venue=r.event_venue, distance=r.distance,
            prog_name=r.prog_name,
            pos_a=r.pos_b, pos_b=r.pos_a,
            finish_status_a=r.finish_status_b, finish_status_b=r.finish_status_a,
            swim_a_sec=r.swim_b_sec, swim_b_sec=r.swim_a_sec,
            t1_a_sec=r.t1_b_sec, t1_b_sec=r.t1_a_sec,
            bike_a_sec=r.bike_b_sec, bike_b_sec=r.bike_a_sec,
            t2_a_sec=r.t2_b_sec, t2_b_sec=r.t2_a_sec,
            run_a_sec=r.run_b_sec, run_b_sec=r.run_a_sec,
            total_a_sec=r.total_b_sec, total_b_sec=r.total_a_sec,
            gap_sec=(-r.gap_sec if r.gap_sec is not None else None),
            winner=("b" if r.winner == "a" else ("a" if r.winner == "b" else None)),
            swim_pack_a=r.swim_pack_b, swim_pack_size_a=r.swim_pack_size_b,
            bike_pack_a=r.bike_pack_b, bike_pack_size_a=r.bike_pack_size_b,
            swim_pack_b=r.swim_pack_a, swim_pack_size_b=r.swim_pack_size_a,
            bike_pack_b=r.bike_pack_a, bike_pack_size_b=r.bike_pack_size_a,
        )
        for r in bundle.race_log
    ]
    ctx = bundle.context
    ctx_swapped = None
    if ctx is not None:
        ctx_swapped = ContextH2H(sections=[
            ContextSection(
                title=s.title,
                note=s.note,
                rows=[
                    SubsetRecord(label=r.label, matches=r.matches,
                                 wins_a=r.wins_b, wins_b=r.wins_a)
                    for r in s.rows
                ],
            )
            for s in ctx.sections
        ])
    return CompareBundle(
        athlete_a=bundle.athlete_b,
        athlete_b=bundle.athlete_a,
        record=rec_swapped,
        how_they_win=htw_swapped,
        split_averages=sa_swapped,
        pack_profile=pp_swapped,
        transitions=tr_swapped,
        race_log=log_swapped,
        context=ctx_swapped,
        has_any_shared=bundle.has_any_shared,
        notes=list(bundle.notes),
    )


def _swap_to_sorted(bundle: CompareBundle, sorted_key: tuple[int, int]) -> CompareBundle:
    """Ensure the cached bundle has athlete_a = sorted_key[0]."""
    if bundle.athlete_a.athlete_id == sorted_key[0]:
        return bundle
    return _swap_bundle(bundle)
