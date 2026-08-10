"""Country medal-table service for the ``/medals`` page.

Aggregates World Triathlon podium finishes (finish_position 1/2/3) into a
per-country gold/silver/bronze table, with filters for gender, time period,
race category (WTCS / World Cup / Continental / Olympic) and para vs able-bodied.

Data source is the triathlon-db (same engine as the /compare page). We fetch
all medal rows once, cache them with a TTL, then filter + aggregate in pandas
per request — the medal universe is small (~16k rows) so this is cheap and
keeps the filter logic readable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from threading import Lock
import time as _time

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Filter vocabularies (kept aligned with the template checkbox values)
# ---------------------------------------------------------------------------

CATEGORY_KEYS: tuple[str, ...] = ("wtcs", "world_cup", "continental", "olympic")
CATEGORY_LABELS: dict[str, str] = {
    "wtcs": "WTCS",
    "world_cup": "World Cup",
    "continental": "Continental",
    "olympic": "Olympic Games",
}

GENDER_KEYS: tuple[str, ...] = ("all", "men", "women")

# Date-range presets: URL value -> years_back
SINCE_OPTIONS: dict[str, int | None] = {
    "": None, "all": None,
    "1y": 1, "2y": 2, "3y": 3, "4y": 4, "5y": 5, "8y": 8,
}

FIELD_KEYS: tuple[str, ...] = ("elite", "junior", "para")
FIELD_LABELS: dict[str, str] = {
    "elite": "Elite",
    "junior": "Junior",
    "para": "Para",
}
DEFAULT_FIELDS: frozenset[str] = frozenset({"elite"})

# Able-bodied senior programs that count toward the elite medal table.
# U23 / Mixed Relay are not currently bucketed into any field option.
_ELITE_PROGS = ("elite men", "elite women")

# Medal colours (also referenced by the template, kept here as the source of truth)
GOLD = "#e8b923"
SILVER = "#a9b4c2"
BRONZE = "#c8823c"


# ---------------------------------------------------------------------------
# Category classification (name-based, same spirit as head_to_head.py but with
# an added ``olympic`` bucket for the Olympic/Paralympic Games).
# ---------------------------------------------------------------------------

# World Triathlon has renamed race tiers over the years — e.g. "ITU Triathlon
# World Cup" (pre-2021) became "World Triathlon Cup" (word order flipped), and
# continental cups picked up qualifiers like "Premium" ("Africa Triathlon
# Premium Cup"). Patterns below are regexes so they tolerate both word order
# and inserted words, rather than requiring an exact literal substring.
_CONTINENTAL_PATTERNS = ("continental championship", "continental cup")

# Old regional-federation acronyms (ETU, OTU, ATU, ASTC, NATU, PATCO) must be
# matched as whole words — a plain substring check false-matches city names
# like "Huatulco" (contains "atu"), which previously miscategorized races.
_CONTINENTAL_ACRONYM_RE = re.compile(r"\b(patco|atu|otu|astc|etu|natu)\b")

# Continent name ... "cup"/"championship(s)" anywhere later in the string —
# covers "Europe Triathlon Cup", "Africa Triathlon Premium Cup",
# "Oceania Triathlon Sprint Championships", etc.
_CONTINENTAL_PHRASE_RE = re.compile(
    r"\b(europe|european|africa|african|asia|asian|americas|oceania)\b"
    r".*\b(cup|championships?)\b"
)

# "World ... Cup" anywhere in the string — covers both the old "ITU
# Triathlon World Cup" ordering and the current "World Triathlon Cup" one
# (checked only after WTCS/Olympic have already been ruled out above).
_WORLD_CUP_RE = re.compile(r"\bworld\b.*\bcup\b")


def classify_medal_category(event_name: object, prog_name: object) -> str:
    """Bucket an event into {wtcs, world_cup, continental, olympic, other}."""
    en = str(event_name or "").lower()
    pn = str(prog_name or "").lower()
    blob = f"{en} {pn}"
    if "olympic games" in blob or "paralympic games" in blob:
        return "olympic"
    if any(s in blob for s in (
        "championship series", "championship finals", "grand final", "wtcs",
    )):
        return "wtcs"
    if _WORLD_CUP_RE.search(blob):
        return "world_cup"
    if (
        _CONTINENTAL_ACRONYM_RE.search(blob)
        or _CONTINENTAL_PHRASE_RE.search(blob)
        or any(s in blob for s in _CONTINENTAL_PATTERNS)
    ):
        return "continental"
    return "other"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MedalRow:
    country: str
    gold: int
    silver: int
    bronze: int

    @property
    def total(self) -> int:
        return self.gold + self.silver + self.bronze


@dataclass
class MedalTable:
    rows: list[MedalRow]          # sorted by total desc, then gold, silver
    by_gold: list[MedalRow]       # sorted by gold desc, then silver, bronze, total
    total_medals: int
    n_countries: int
    max_total: int                # for bar scaling
    max_gold: int
    filters: dict = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.rows)


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

_MEDAL_SQL = text("""
    SELECT
        a.country       AS country,
        a.gender        AS gender,
        rr.finish_position AS pos,
        e.prog_name     AS prog_name,
        e.event_name    AS event_name,
        e.event_date    AS event_date
    FROM race_results rr
    JOIN events  e ON e.event_id = rr.event_id AND e.prog_id = rr.prog_id
    JOIN athlete a ON a.athlete_id = rr.athlete_id
    WHERE rr.finish_position BETWEEN 1 AND 3
      AND a.country IS NOT NULL AND a.country <> ''
      AND lower(coalesce(e.prog_name, '')) <> 'mixed relay'
""")

_cache_df: pd.DataFrame | None = None
_cache_ts: float = 0.0
_cache_lock = Lock()
_CACHE_TTL_SEC = 3600


def _fetch_all_medals(engine: Engine) -> pd.DataFrame:
    """Fetch every individual podium finish and enrich with category / para flags."""
    with engine.connect() as conn:
        df = pd.read_sql(_MEDAL_SQL, conn)
    if df.empty:
        return df
    df["prog_lower"] = df["prog_name"].astype(str).str.strip().str.lower()
    df["is_para"] = df["prog_lower"].str.startswith("pt")
    df["is_elite"] = df["prog_lower"].isin(_ELITE_PROGS)
    df["is_junior"] = df["prog_lower"].str.contains("junior", na=False)
    df["category"] = [
        classify_medal_category(en, pn)
        for en, pn in zip(df["event_name"], df["prog_name"])
    ]
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
    df["gender"] = df["gender"].astype(str).str.strip().str.lower()
    return df


def _get_medals_df(engine: Engine, *, use_cache: bool = True) -> pd.DataFrame:
    global _cache_df, _cache_ts
    now = _time.time()
    if use_cache and _cache_df is not None and (now - _cache_ts) < _CACHE_TTL_SEC:
        return _cache_df
    with _cache_lock:
        if not use_cache or _cache_df is None or (_time.time() - _cache_ts) >= _CACHE_TTL_SEC:
            _cache_df = _fetch_all_medals(engine)
            _cache_ts = _time.time()
        return _cache_df


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_medal_table(
    engine: Engine,
    *,
    gender: str = "all",
    years_back: int | None = None,
    categories: set[str] | None = None,
    fields: set[str] | None = None,
    use_cache: bool = True,
) -> MedalTable:
    """Assemble the filtered country medal table."""
    df = _get_medals_df(engine, use_cache=use_cache)
    flds = fields if fields else set(DEFAULT_FIELDS)
    filters = {
        "gender": gender, "years_back": years_back,
        "categories": sorted(categories) if categories else [], "fields": sorted(flds),
    }
    if df is None or df.empty:
        return MedalTable([], [], 0, 0, 0, 0, filters)

    m = df

    # Field: elite / junior / para are independent buckets, OR'd together
    mask = pd.Series(False, index=m.index)
    if "elite" in flds:
        mask |= m["is_elite"]
    if "junior" in flds:
        mask |= m["is_junior"]
    if "para" in flds:
        mask |= m["is_para"]
    m = m[mask]

    # Gender
    if gender == "men":
        m = m[m["gender"] == "male"]
    elif gender == "women":
        m = m[m["gender"] == "female"]

    # Race category (empty => all)
    if categories:
        cats = {c.strip().lower() for c in categories if c}
        if cats:
            m = m[m["category"].isin(cats)]

    # Time period
    if years_back is not None:
        today = date.today()
        cutoff = today.replace(year=today.year - int(years_back))
        m = m[m["event_date"].dt.date >= cutoff]

    if m.empty:
        return MedalTable([], [], 0, 0, 0, 0, filters)

    # Aggregate: one row per country with gold/silver/bronze counts
    agg = (
        m.assign(
            gold=(m["pos"] == 1).astype(int),
            silver=(m["pos"] == 2).astype(int),
            bronze=(m["pos"] == 3).astype(int),
        )
        .groupby("country", as_index=False)[["gold", "silver", "bronze"]]
        .sum()
    )
    agg["total"] = agg["gold"] + agg["silver"] + agg["bronze"]

    rows_total = agg.sort_values(
        ["total", "gold", "silver", "country"],
        ascending=[False, False, False, True],
    )
    rows_gold = agg.sort_values(
        ["gold", "silver", "bronze", "total", "country"],
        ascending=[False, False, False, False, True],
    )

    def _to_rows(frame: pd.DataFrame) -> list[MedalRow]:
        return [
            MedalRow(str(r.country), int(r.gold), int(r.silver), int(r.bronze))
            for r in frame.itertuples(index=False)
        ]

    rows = _to_rows(rows_total)
    by_gold = _to_rows(rows_gold)

    return MedalTable(
        rows=rows,
        by_gold=by_gold,
        total_medals=int(agg["total"].sum()),
        n_countries=len(rows),
        max_total=int(agg["total"].max()),
        max_gold=int(agg["gold"].max()),
        filters=filters,
    )
