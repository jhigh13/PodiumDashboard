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
from app.utils.timefmt import distance_sort_key, time_to_seconds


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


FRONT_SWIM_GAP_SEC = 30
FRONT_BIKE_GAP_SEC = 15


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
    total_a_sec: float | None
    total_b_sec: float | None
    gap_sec: float | None  # total_a - total_b
    winner: str | None     # 'a' | 'b' | None


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
    # Diagnostics
    has_any_shared: bool = True
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_athlete(token: str | int | None) -> AthleteRef | None:
    """Resolve a slug or numeric ID to an AthleteRef via the index."""
    if token is None or token == "":
        return None
    idx = get_athlete_index()
    if isinstance(token, int):
        entry = idx.by_id(token)
    else:
        s = str(token).strip()
        if s.isdigit():
            entry = idx.by_id(int(s))
        else:
            entry = idx.by_slug(s)
    return AthleteRef.from_entry(entry) if entry else None


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


def _shared_races_df(
    races_a: pd.DataFrame,
    races_b: pd.DataFrame,
    *,
    include_mixed_relay: bool = False,
    include_para: bool = False,
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
    return merged.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------


def _build_record(shared: pd.DataFrame) -> H2HRecord:
    if shared.empty:
        return H2HRecord(0, 0, 0, 0, None, None, None, None, None)
    finished = shared[_finished_mask(shared, "_a") & _finished_mask(shared, "_b")].copy()
    if finished.empty:
        return H2HRecord(0, 0, 0, 0, None, None, None, None, None)
    a_total = finished["total_time_sec_a"]
    b_total = finished["total_time_sec_b"]
    valid = a_total.notna() & b_total.notna()
    finished = finished[valid].copy()
    if finished.empty:
        return H2HRecord(0, 0, 0, 0, None, None, None, None, None)

    a_total = finished["total_time_sec_a"]
    b_total = finished["total_time_sec_b"]
    wins_a = int((a_total < b_total).sum())
    wins_b = int((b_total < a_total).sum())
    ties = int((a_total == b_total).sum())
    matches = len(finished)
    avg_gap = float((a_total - b_total).mean())

    # Latest shared race (use event_date when available, otherwise first row).
    last_idx = None
    if "event_date" in finished.columns and finished["event_date"].notna().any():
        last_idx = finished["event_date"].idxmax()
    else:
        last_idx = finished.index[-1]
    last_row = finished.loc[last_idx]
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
) -> list[SplitAverages]:
    """Per-distance averages computed independently for each athlete.

    Note: NOT restricted to shared races — uses each athlete's individual
    record over the last ``years_back`` years, with IQR outlier removal
    per (athlete, distance, segment). This matches the Pro Tri News card.
    """
    today = date.today()
    cutoff = today.replace(year=today.year - years_back)

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


def _build_race_log(shared: pd.DataFrame) -> list[RaceLogRow]:
    if shared.empty:
        return []
    df = shared.copy()
    # Sort newest first
    if "event_date" in df.columns:
        df = df.sort_values("event_date", ascending=False, na_position="last")
    rows: list[RaceLogRow] = []
    for _, r in df.iterrows():
        a_fin = str(r.get("finish_status_a") or "").upper().strip()
        b_fin = str(r.get("finish_status_b") or "").upper().strip()
        ta = r.get("total_time_sec_a")
        tb = r.get("total_time_sec_b")
        ta = float(ta) if pd.notna(ta) else None
        tb = float(tb) if pd.notna(tb) else None
        gap = (ta - tb) if (ta is not None and tb is not None) else None
        winner: str | None = None
        if a_fin in _FINISHED_STATUSES and b_fin in _FINISHED_STATUSES and ta is not None and tb is not None:
            if ta < tb:
                winner = "a"
            elif tb < ta:
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
            total_a_sec=ta,
            total_b_sec=tb,
            gap_sec=gap,
            winner=winner,
        ))
    return rows


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


_BUNDLE_CACHE: dict[tuple[int, int], tuple[float, CompareBundle]] = {}
_CACHE_TTL_SEC = 3600
_CACHE_MAX = 256


def _cache_get(key: tuple[int, int]) -> CompareBundle | None:
    entry = _BUNDLE_CACHE.get(key)
    if not entry:
        return None
    ts, bundle = entry
    if (_time.time() - ts) > _CACHE_TTL_SEC:
        _BUNDLE_CACHE.pop(key, None)
        return None
    return bundle


def _cache_put(key: tuple[int, int], bundle: CompareBundle) -> None:
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
) -> CompareBundle:
    """Top-level: fetch + assemble all cards for the (a, b) athlete pair."""
    if a_id == b_id:
        raise ValueError("Athletes must be different")
    key = tuple(sorted((int(a_id), int(b_id))))
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
    shared = _shared_races_df(races_a, races_b)

    if shared.empty:
        bundle = CompareBundle(
            athlete_a=AthleteRef.from_entry(a_entry),
            athlete_b=AthleteRef.from_entry(b_entry),
            record=H2HRecord(0, 0, 0, 0, None, None, None, None, None),
            how_they_win=HowTheyWin(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            split_averages=_build_split_averages(races_a, races_b),
            pack_profile=[_empty_pack_profile(int(a_id)), _empty_pack_profile(int(b_id))],
            transitions=TransitionH2H(0, 0, 0, 0, 0, 0, None, None, None, None, None, None),
            race_log=[],
            has_any_shared=False,
        )
        if use_cache:
            _cache_put(key, _swap_to_sorted(bundle, key))
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
        split_averages=_build_split_averages(races_a, races_b),
        pack_profile=[
            _build_pack_profile(int(a_id), pack_all),
            _build_pack_profile(int(b_id), pack_all),
        ],
        transitions=_build_transitions(shared),
        race_log=_build_race_log(shared),
        has_any_shared=True,
    )
    if use_cache:
        _cache_put(key, _swap_to_sorted(bundle, key))
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
            total_a_sec=r.total_b_sec, total_b_sec=r.total_a_sec,
            gap_sec=(-r.gap_sec if r.gap_sec is not None else None),
            winner=("b" if r.winner == "a" else ("a" if r.winner == "b" else None)),
        )
        for r in bundle.race_log
    ]
    return CompareBundle(
        athlete_a=bundle.athlete_b,
        athlete_b=bundle.athlete_a,
        record=rec_swapped,
        how_they_win=htw_swapped,
        split_averages=sa_swapped,
        pack_profile=pp_swapped,
        transitions=tr_swapped,
        race_log=log_swapped,
        has_any_shared=bundle.has_any_shared,
        notes=list(bundle.notes),
    )


def _swap_to_sorted(bundle: CompareBundle, sorted_key: tuple[int, int]) -> CompareBundle:
    """Ensure the cached bundle has athlete_a = sorted_key[0]."""
    if bundle.athlete_a.athlete_id == sorted_key[0]:
        return bundle
    return _swap_bundle(bundle)
