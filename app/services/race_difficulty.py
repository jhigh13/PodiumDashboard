"""Race Difficulty analysis service.

Pure logic for the coach "Race Difficulty" tab: find the unique World Triathlon
races a set of roster athletes did in a year, derive each athlete's weight,
extract/trim/downsample bike power streams, and provide the reference score
implementation (the interactive recompute happens client-side in JS).

No FastAPI imports here so everything is unit-testable.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional, Sequence

from sqlalchemy import bindparam, select, text

from app.data.db import get_session
from app.models.tables import Workout, WorkoutDetail
from app.services.workout_cache import normalize_timeseries_rows


# ── Small helpers mirrored from app.py (nested in create_app, not importable) ─

def parse_event_prog_key(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or ":" not in s:
        return None
    a, b = s.split(":", 1)
    try:
        return int(a), int(b)
    except Exception:
        return None


def _sport_norm(value: str | None) -> str:
    return (value or "").strip().lower()


def sport_matches(workout_sport: str | None, desired: str) -> bool:
    ws = _sport_norm(workout_sport)
    d = _sport_norm(desired)
    if not d:
        return True
    if d == "run":
        return ws in {"run", "running"}
    if d == "bike":
        return ws in {"bike", "cycling", "ride"}
    if d == "swim":
        return ws in {"swim", "swimming"}
    return ws == d


def pick_default_workout(workouts: list[dict], desired_sport: str) -> str | None:
    for w in workouts:
        if w.get("workout_id") and sport_matches(w.get("sport"), desired_sport):
            return str(w["workout_id"])
    for w in workouts:
        if w.get("workout_id"):
            return str(w["workout_id"])
    return None


def find_channel_index(channels: list[str], candidates: set[str]) -> int | None:
    if not channels:
        return None
    for i, name in enumerate(channels):
        n = (name or "").strip().lower().replace(" ", "")
        if n in candidates:
            return i
    for i, name in enumerate(channels):
        n = (name or "").strip().lower().replace(" ", "")
        for c in candidates:
            if c in n:
                return i
    return None


# ── Race listing (live against triathlon-db) ─────────────────────────────────

_UNION_RACES_SQL = text(
    """
    SELECT
        rr.event_id,
        rr.prog_id,
        rr.athlete_id,
        ev.event_date,
        ev.event_name,
        ev.event_venue,
        ev.prog_name,
        ev.prog_distance_category
    FROM public.race_results rr
    JOIN public.events ev
        ON ev.event_id = rr.event_id
       AND ev.prog_id = rr.prog_id
    WHERE rr.athlete_id IN :wto_ids
      AND ev.event_date >= :start_date
      AND ev.event_date <= :end_date
    ORDER BY ev.event_date
    """
).bindparams(bindparam("wto_ids", expanding=True))


def group_race_rows(rows: Sequence[dict], podium_by_wto_id: dict[int, dict]) -> list[dict]:
    """De-dupe race rows by (event_id, prog_id) and collect participants.

    ``rows``: mappings with keys event_id, prog_id, athlete_id (WTO id),
    event_date, event_name, event_venue, prog_name, prog_distance_category.
    ``podium_by_wto_id``: WTO athlete id -> {"podium_athlete_id", "name"}.
    """
    grouped: dict[tuple[int, int], dict] = {}
    for r in rows:
        key = (int(r["event_id"]), int(r["prog_id"]))
        g = grouped.get(key)
        if g is None:
            g = {
                "event_id": key[0],
                "prog_id": key[1],
                "key": f"{key[0]}:{key[1]}",
                "event_date": r["event_date"],
                "event_name": r["event_name"],
                "event_venue": r["event_venue"],
                "prog_name": r["prog_name"],
                "prog_distance_category": r["prog_distance_category"],
                "participants": [],
                "participant_podium_ids": [],
            }
            grouped[key] = g
        p = podium_by_wto_id.get(int(r["athlete_id"]))
        if p and p["podium_athlete_id"] not in g["participant_podium_ids"]:
            g["participant_podium_ids"].append(p["podium_athlete_id"])
            g["participants"].append(p["name"])
    return sorted(grouped.values(), key=lambda g: (g["event_date"], g["key"]))


def fetch_union_races(mappings: list[dict], year: int, engine) -> list[dict]:
    """Unique races (event_id, prog_id) raced in ``year`` by any mapped athlete.

    ``mappings``: [{"podium_athlete_id", "name", "wto_athlete_id"}, ...]
    """
    wto_ids = [int(m["wto_athlete_id"]) for m in mappings if m.get("wto_athlete_id")]
    if not wto_ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            _UNION_RACES_SQL,
            {
                "wto_ids": wto_ids,
                "start_date": date(int(year), 1, 1),
                "end_date": date(int(year), 12, 31),
            },
        ).mappings().all()
    podium_by_wto_id = {
        int(m["wto_athlete_id"]): {"podium_athlete_id": int(m["podium_athlete_id"]), "name": m["name"]}
        for m in mappings
        if m.get("wto_athlete_id")
    }
    return group_race_rows(rows, podium_by_wto_id)


def fetch_event_header(event_id: int, prog_id: int, engine) -> dict | None:
    sql = text(
        """
        SELECT event_date, event_name, event_venue, prog_name
        FROM public.events
        WHERE event_id = :event_id AND prog_id = :prog_id
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"event_id": int(event_id), "prog_id": int(prog_id)}).mappings().first()
    return dict(row) if row else None


# ── Weight prefill ────────────────────────────────────────────────────────────

def derive_weight_kg(podium_athlete_id: int) -> float | None:
    """Best-effort weight from the most recent WorkoutDetail with both
    power_average and watts_per_kg set (weight = power / wkg).

    Frequently None: the FIT-parse cache path fills only WorkoutChannels, so
    WorkoutStats-derived rows exist only where the TP details endpoint ran.
    """
    with get_session() as session:
        row = session.execute(
            select(WorkoutDetail.power_average, WorkoutDetail.watts_per_kg)
            .join(Workout, WorkoutDetail.workout_id == Workout.id)
            .where(Workout.athlete_id == int(podium_athlete_id))
            .where(WorkoutDetail.power_average > 0)
            .where(WorkoutDetail.watts_per_kg > 0)
            .order_by(Workout.date.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    try:
        return round(float(row[0]) / float(row[1]), 1)
    except (TypeError, ZeroDivisionError):
        return None


# ── Power stream extraction / trimming / downsampling ────────────────────────

def extract_power_series(payload: dict) -> tuple[list[float], list[Optional[float]], list[float]]:
    """Timeseries payload -> (t_seconds, power_watts, speed_m_s).

    Power values may be None where the sample lacks power; speed defaults to
    0.0 (it is only used by the trim heuristic).
    """
    channels, rows = normalize_timeseries_rows(payload)
    if not rows:
        return [], [], []

    idx_time = find_channel_index(channels, {"millisecondoffset", "time", "seconds", "sec", "elapsedtime", "elapsedseconds"})
    idx_power = find_channel_index(channels, {"power", "watts", "w"})
    idx_speed = find_channel_index(channels, {"speed", "velocity", "vel"})
    if idx_power is None:
        return [], [], []

    time_is_ms = False
    if idx_time is not None:
        ch_name = (channels[idx_time] or "").strip().lower().replace(" ", "")
        time_is_ms = "millisecond" in ch_name

    t: list[float] = []
    power: list[Optional[float]] = []
    speed: list[float] = []
    for i, r in enumerate(rows):
        if idx_time is not None and idx_time < len(r):
            try:
                raw_t = float(r[idx_time])
                t_sec = raw_t / 1000.0 if time_is_ms else raw_t
            except Exception:
                t_sec = float(i)
        else:
            t_sec = float(i)
        t.append(t_sec)

        p = None
        if idx_power < len(r):
            try:
                p = float(r[idx_power]) if r[idx_power] is not None else None
            except Exception:
                p = None
        power.append(p)

        s = 0.0
        if idx_speed is not None and idx_speed < len(r):
            try:
                s = float(r[idx_speed]) if r[idx_speed] is not None else 0.0
            except Exception:
                s = 0.0
        speed.append(s)

    return t, power, speed


def auto_trim_indices(t: list[float], power: list[Optional[float]], speed: list[float]) -> tuple[int, int]:
    """Inclusive (start, end) indices of the "race" portion of a bike stream.

    Race files carry >60 s recording gaps on either side of the bike leg (a
    pre-race pause, or a post-race stop record after a long gap), so "restart
    after the last gap" mis-trims files whose gap comes late. Instead: split
    the stream at >60 s gaps, keep the segment with the most power>0 samples,
    then strip leading/trailing dead time within it. Degenerates safely to the
    full range.
    """
    n = len(t)
    if n == 0:
        return 0, -1

    bounds = [0]
    for i in range(1, n):
        if (t[i] - t[i - 1]) > 60:
            bounds.append(i)
    bounds.append(n)

    seg_a, seg_b = 0, n
    best_score = -1
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        score = sum(1 for i in range(a, b) if (power[i] or 0) > 0)
        if score > best_score:
            best_score = score
            seg_a, seg_b = a, b

    start, end = seg_a, seg_b - 1
    for i in range(seg_a, seg_b):
        if speed[i] > 0 or (power[i] or 0) > 0:
            start = i
            break
    for i in range(seg_b - 1, start - 1, -1):
        if speed[i] > 0 or (power[i] or 0) > 0:
            end = i
            break

    if end < start:
        return 0, n - 1
    return start, end


def downsample_series(t: list[float], wkg: list[Optional[float]], max_points: int = 1200) -> tuple[list[float], list[Optional[float]]]:
    n = len(t)
    if n <= max_points:
        return list(t), list(wkg)
    stride = max(1, int(math.ceil(n / max_points)))
    return t[::stride], wkg[::stride]


def build_athlete_stream(payload: dict, weight_kg: float, max_points: int = 1200) -> tuple[dict | None, str | None]:
    """Compose extraction + auto-trim + W/kg + downsample.

    Returns (stream, None) or (None, reason). Stream keys:
    t (seconds, re-zeroed), wkg, auto_trim {lead_s, trail_s}, duration_s.
    """
    if not weight_kg or weight_kg <= 0:
        return None, "No weight provided"

    t, power, speed = extract_power_series(payload)
    if not t:
        return None, "No power channel in file"
    if not any((p or 0) > 0 for p in power):
        return None, "Power channel is empty"

    start, end = auto_trim_indices(t, power, speed)
    lead_s = t[start] - t[0]
    trail_s = t[-1] - t[end]
    t0 = t[start]
    t_trimmed = [round(x - t0, 1) for x in t[start:end + 1]]
    wkg = [round(p / weight_kg, 3) if p is not None else None for p in power[start:end + 1]]

    t_ds, wkg_ds = downsample_series(t_trimmed, wkg, max_points=max_points)
    return {
        "t": t_ds,
        "wkg": wkg_ds,
        "auto_trim": {"lead_s": round(lead_s), "trail_s": round(trail_s)},
        "duration_s": round(t_trimmed[-1]) if t_trimmed else 0,
    }, None


# ── Score reference implementation ────────────────────────────────────────────
# The live recompute happens client-side; this is the canonical formula the JS
# mirrors, kept here so tests can pin expected values.

def compute_scores(values: list[Optional[float]]) -> list[Optional[float]]:
    """Normalized 0-100 difficulty scores across the group.

    Score = (Value - (AvgGroup - 3*sd)) / ((6*sd)/100), clamped to [0, 100],
    with sd the sample standard deviation (ddof=1) of the athletes' values.
    Degenerate groups (fewer than 2 values, or sd == 0) score 50 across the
    board. None inputs stay None.
    """
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return [50.0 if v is not None else None for v in values]

    mean = sum(present) / len(present)
    var = sum((v - mean) ** 2 for v in present) / (len(present) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return [50.0 if v is not None else None for v in values]

    lower = mean - 3 * sd
    denom = (6 * sd) / 100.0
    out: list[Optional[float]] = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            out.append(max(0.0, min(100.0, (v - lower) / denom)))
    return out
