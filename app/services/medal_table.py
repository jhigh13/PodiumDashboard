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

# Categorical palette for the per-country trend line chart (validated
# CVD-safe order for lines/adjacent-pair charts, up to 8 series).
TREND_PALETTE: tuple[str, ...] = (
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
)
TREND_TOP_N = 8


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
    athletes: int = 0   # count of distinct medalists behind these medals

    @property
    def total(self) -> int:
        return self.gold + self.silver + self.bronze

    @property
    def medals_per_athlete(self) -> float:
        """>1 means some athletes medaled more than once; 1.0 = every medal a different athlete."""
        return (self.total / self.athletes) if self.athletes else 0.0


@dataclass
class AthleteMedalRow:
    athlete_id: int
    name: str
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


@dataclass
class MedalTrendSeries:
    country: str
    color: str
    totals: list[int]     # per-year medal count, aligned to MedalTrend.years (NOT cumulative)


@dataclass
class MedalTrend:
    years: list[int]
    series: list[MedalTrendSeries]
    n_countries_total: int        # how many countries had a medal at all under these filters
    filters: dict = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.years) and bool(self.series)

    @property
    def other_included(self) -> bool:
        return self.n_countries_total > len(self.series)


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

_MEDAL_SQL = text("""
    SELECT
        a.athlete_id    AS athlete_id,
        a.full_name     AS full_name,
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


def _apply_field_gender_category(
    m: pd.DataFrame,
    *,
    gender: str,
    categories: set[str] | None,
    fields: set[str],
) -> pd.DataFrame:
    """Shared filter core: field (elite/junior/para), gender, race category.

    Time period is deliberately left out — callers apply it (or not) themselves,
    since the trend view needs the full date range while the table view doesn't.
    """
    # Field: elite / junior / para are independent buckets, OR'd together
    mask = pd.Series(False, index=m.index)
    if "elite" in fields:
        mask |= m["is_elite"]
    if "junior" in fields:
        mask |= m["is_junior"]
    if "para" in fields:
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

    return m


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

    m = _apply_field_gender_category(df, gender=gender, categories=categories, fields=flds)

    # Time period
    if years_back is not None:
        today = date.today()
        cutoff = today.replace(year=today.year - int(years_back))
        m = m[m["event_date"].dt.date >= cutoff]

    if m.empty:
        return MedalTable([], [], 0, 0, 0, 0, filters)

    # Aggregate: one row per country with gold/silver/bronze counts + medalist depth
    agg = (
        m.assign(
            gold=(m["pos"] == 1).astype(int),
            silver=(m["pos"] == 2).astype(int),
            bronze=(m["pos"] == 3).astype(int),
        )
        .groupby("country", as_index=False)
        .agg(
            gold=("gold", "sum"),
            silver=("silver", "sum"),
            bronze=("bronze", "sum"),
            athletes=("athlete_id", "nunique"),
        )
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
            MedalRow(str(r.country), int(r.gold), int(r.silver), int(r.bronze), int(r.athletes))
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


def build_medal_trend(
    engine: Engine,
    *,
    gender: str = "all",
    categories: set[str] | None = None,
    fields: set[str] | None = None,
    top_n: int = TREND_TOP_N,
    use_cache: bool = True,
) -> MedalTrend:
    """Per-year (non-cumulative) medal counts for the top-N countries.

    Deliberately ignores the "since" time-period filter — a multi-year trend
    is the point, so this always spans the full history available under the
    other filters. Country order (and therefore color, assigned by position)
    reflects each country's rank *within these filters*, so it can shift
    between filter combinations — trade-off noted where this is rendered.
    """
    df = _get_medals_df(engine, use_cache=use_cache)
    flds = fields if fields else set(DEFAULT_FIELDS)
    filters = {
        "gender": gender,
        "categories": sorted(categories) if categories else [],
        "fields": sorted(flds),
    }
    if df is None or df.empty:
        return MedalTrend([], [], 0, filters)

    m = _apply_field_gender_category(df, gender=gender, categories=categories, fields=flds)
    m = m.dropna(subset=["event_date"])
    if m.empty:
        return MedalTrend([], [], 0, filters)

    m = m.assign(year=m["event_date"].dt.year.astype(int))

    totals_by_country = m.groupby("country").size().sort_values(ascending=False)
    n_countries_total = len(totals_by_country)
    top_countries = list(totals_by_country.index[:top_n])
    if not top_countries:
        return MedalTrend([], [], n_countries_total, filters)

    years = sorted(m["year"].unique().tolist())

    per_year = (
        m[m["country"].isin(top_countries)]
        .groupby(["country", "year"])
        .size()
    )

    series = [
        MedalTrendSeries(
            country=country,
            color=TREND_PALETTE[i % len(TREND_PALETTE)],
            totals=[int(per_year.get((country, y), 0)) for y in years],
        )
        for i, country in enumerate(top_countries)
    ]

    return MedalTrend(
        years=years,
        series=series,
        n_countries_total=n_countries_total,
        filters=filters,
    )


def build_country_medalists(
    engine: Engine,
    country: str,
    *,
    gender: str = "all",
    years_back: int | None = None,
    categories: set[str] | None = None,
    fields: set[str] | None = None,
    use_cache: bool = True,
) -> list[AthleteMedalRow]:
    """Per-athlete medal breakdown for one country, under the same filters as
    the main table — the drill-down behind each expandable row.
    """
    df = _get_medals_df(engine, use_cache=use_cache)
    flds = fields if fields else set(DEFAULT_FIELDS)
    if df is None or df.empty:
        return []

    m = _apply_field_gender_category(df, gender=gender, categories=categories, fields=flds)
    m = m[m["country"] == country]

    if years_back is not None:
        today = date.today()
        cutoff = today.replace(year=today.year - int(years_back))
        m = m[m["event_date"].dt.date >= cutoff]

    if m.empty:
        return []

    agg = (
        m.assign(
            gold=(m["pos"] == 1).astype(int),
            silver=(m["pos"] == 2).astype(int),
            bronze=(m["pos"] == 3).astype(int),
        )
        .groupby(["athlete_id", "full_name"], as_index=False)
        .agg(gold=("gold", "sum"), silver=("silver", "sum"), bronze=("bronze", "sum"))
    )
    agg["total"] = agg["gold"] + agg["silver"] + agg["bronze"]
    agg = agg.sort_values(
        ["total", "gold", "silver", "full_name"],
        ascending=[False, False, False, True],
    )

    return [
        AthleteMedalRow(
            athlete_id=int(r.athlete_id),
            name=str(r.full_name) if r.full_name else "(unknown athlete)",
            gold=int(r.gold), silver=int(r.silver), bronze=int(r.bronze),
        )
        for r in agg.itertuples(index=False)
    ]
