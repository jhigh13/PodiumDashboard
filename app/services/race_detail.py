"""Per-race detail view for the /compare/race/{event_id}/{prog_id} page.

Reuses the same triathlon-db tables as the compare service. One service call
returns everything the detail page needs: event metadata, conditions, podium,
and per-athlete stage data for the two athletes being compared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.utils.timefmt import distance_label, time_to_seconds


_FINISHED = ("FINISH", "FINISHED", "OK", "OFFICIAL")


@dataclass
class EventInfo:
    event_id: int
    prog_id: int
    event_name: str
    prog_name: str | None
    distance: str | None          # canonical label (e.g. "Sprint", "Olympic")
    event_date: date | None
    event_venue: str | None
    event_country: str | None
    # All distances stored as meters in the DB; template converts as needed
    swim_distance_m: float | None
    bike_distance_m: float | None
    run_distance_m: float | None


@dataclass
class Conditions:
    """Race-day environmental conditions; all fields optional."""
    temperature_water_c: float | None
    temperature_air_c: float | None
    wbgt_c: float | None
    wetsuit: str | None
    weather: str | None
    wind: str | None
    wind_speed_kmh: float | None
    precipitation_mm: float | None


@dataclass
class PodiumEntry:
    rank: int                     # 1, 2, 3
    athlete_id: int | None
    full_name: str
    country: str | None
    total_time_sec: float | None


@dataclass
class RaceAthleteData:
    athlete_id: int
    full_name: str
    country: str | None
    finish_position: int | None
    finish_status: str | None
    # Segment times in seconds (parsed from HH:MM:SS strings)
    swim_sec: float | None
    t1_sec: float | None
    bike_sec: float | None
    t2_sec: float | None
    run_sec: float | None
    total_sec: float | None
    # Positions at each checkpoint (rank in field; 1 = leader)
    pos_swim: int | None
    pos_t1: int | None
    pos_bike: int | None
    pos_t2: int | None
    pos_run: int | None
    # Gap to leader at each checkpoint (cumulative, in seconds)
    gap_swim: float | None
    gap_t1: float | None
    gap_bike: float | None
    gap_t2: float | None
    gap_run: float | None
    # Per-segment ranks (rank of this segment time alone within field)
    rank_swim: int | None
    rank_t1: int | None
    rank_bike: int | None
    rank_t2: int | None
    rank_run: int | None


@dataclass
class RaceDetailBundle:
    event: EventInfo
    conditions: Conditions
    podium: list[PodiumEntry]
    athlete_a: RaceAthleteData | None
    athlete_b: RaceAthleteData | None


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _to_float(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(str(v).strip())
        return f if f == f else None
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _coerce_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, pd.Timestamp):
        return v.date()
    try:
        return date.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


def _fetch_event(event_id: int, prog_id: int, engine: Engine) -> EventInfo | None:
    sql = text("""
        SELECT event_id, prog_id, event_name, prog_name, prog_distance_category,
               event_date, event_venue, event_country,
               swim_distance, bike_distance, run_distance
        FROM events
        WHERE event_id = :eid AND prog_id = :pid
        LIMIT 1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"eid": int(event_id), "pid": int(prog_id)}).first()
    if row is None:
        return None
    return EventInfo(
        event_id=int(row.event_id),
        prog_id=int(row.prog_id),
        event_name=str(row.event_name or ""),
        prog_name=str(row.prog_name or "") or None,
        distance=distance_label(row.prog_distance_category) if row.prog_distance_category else None,
        event_date=_coerce_date(row.event_date),
        event_venue=str(row.event_venue or "") or None,
        event_country=str(row.event_country or "") or None,
        swim_distance_m=_to_float(row.swim_distance),
        bike_distance_m=_to_float(row.bike_distance),
        run_distance_m=_to_float(row.run_distance),
    )


def _fetch_conditions(event_id: int, prog_id: int, engine: Engine) -> Conditions:
    sql = text("""
        SELECT temperature_water, temperature_air, wbgt, wetsuit, weather,
               wind, wind_speed_kmh, precipitation_mm
        FROM events
        WHERE event_id = :eid AND prog_id = :pid
        LIMIT 1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"eid": int(event_id), "pid": int(prog_id)}).first()
    if row is None:
        return Conditions(None, None, None, None, None, None, None, None)
    return Conditions(
        temperature_water_c=_to_float(row.temperature_water),
        temperature_air_c=_to_float(row.temperature_air),
        wbgt_c=_to_float(row.wbgt),
        wetsuit=str(row.wetsuit or "") or None,
        weather=str(row.weather or "") or None,
        wind=str(row.wind or "") or None,
        wind_speed_kmh=_to_float(row.wind_speed_kmh),
        precipitation_mm=_to_float(row.precipitation_mm),
    )


def _fetch_podium(event_id: int, prog_id: int, engine: Engine) -> list[PodiumEntry]:
    sql = text("""
        SELECT rr.athlete_id, COALESCE(a.full_name, rr.athlete_full_name) AS full_name,
               a.country, rr.total_time, rr.finish_position
        FROM race_results rr
        LEFT JOIN athlete a ON a.athlete_id = rr.athlete_id
        WHERE rr.event_id = :eid AND rr.prog_id = :pid
          AND UPPER(rr.finish_status) IN ('FINISH','FINISHED','OK','OFFICIAL')
          AND rr.finish_position IS NOT NULL
        ORDER BY rr.finish_position ASC
        LIMIT 3
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"eid": int(event_id), "pid": int(prog_id)}).fetchall()
    return [
        PodiumEntry(
            rank=int(r.finish_position) if r.finish_position is not None else (idx + 1),
            athlete_id=_to_int(r.athlete_id),
            full_name=str(r.full_name or ""),
            country=str(r.country or "") or None,
            total_time_sec=time_to_seconds(r.total_time),
        )
        for idx, r in enumerate(rows)
    ]


def _fetch_athletes(event_id: int, prog_id: int, a_id: int, b_id: int, engine: Engine) -> dict[int, RaceAthleteData]:
    sql = text("""
        SELECT rr.athlete_id,
               COALESCE(a.full_name, rr.athlete_full_name) AS full_name,
               a.country,
               rr.swimtime, rr.t1time, rr.biketime, rr.t2time, rr.runtime,
               rr.total_time, rr.finish_status, rr.finish_position,
               pm.elapsedswim, pm.elapsedt1, pm.elapsedbike, pm.elapsedt2, pm.elapsedrun,
               pm.behindswim, pm.behindt1, pm.behindbike, pm.behindt2, pm.behindrun,
               pm.position_at_swim, pm.position_at_t1, pm.position_at_bike,
               pm.position_at_t2, pm.position_at_run,
               pm.swimrank, pm.t1rank, pm.bikerank, pm.t2rank, pm.runrank
        FROM race_results rr
        LEFT JOIN athlete a ON a.athlete_id = rr.athlete_id
        LEFT JOIN position_metrics pm
          ON pm.athlete_id = rr.athlete_id
         AND pm.event_id  = rr.event_id
         AND pm.prog_id   = rr.prog_id
        WHERE rr.event_id = :eid AND rr.prog_id = :pid
          AND rr.athlete_id IN (:aid, :bid)
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "eid": int(event_id), "pid": int(prog_id),
            "aid": int(a_id), "bid": int(b_id),
        }).fetchall()

    out: dict[int, RaceAthleteData] = {}
    for r in rows:
        out[int(r.athlete_id)] = RaceAthleteData(
            athlete_id=int(r.athlete_id),
            full_name=str(r.full_name or ""),
            country=str(r.country or "") or None,
            finish_position=_to_int(r.finish_position),
            finish_status=(str(r.finish_status or "").upper() or None),
            swim_sec=time_to_seconds(r.swimtime),
            t1_sec=time_to_seconds(r.t1time),
            bike_sec=time_to_seconds(r.biketime),
            t2_sec=time_to_seconds(r.t2time),
            run_sec=time_to_seconds(r.runtime),
            total_sec=time_to_seconds(r.total_time),
            pos_swim=_to_int(r.position_at_swim),
            pos_t1=_to_int(r.position_at_t1),
            pos_bike=_to_int(r.position_at_bike),
            pos_t2=_to_int(r.position_at_t2),
            pos_run=_to_int(r.position_at_run),
            gap_swim=_to_float(r.behindswim),
            gap_t1=_to_float(r.behindt1),
            gap_bike=_to_float(r.behindbike),
            gap_t2=_to_float(r.behindt2),
            gap_run=_to_float(r.behindrun),
            rank_swim=_to_int(r.swimrank),
            rank_t1=_to_int(r.t1rank),
            rank_bike=_to_int(r.bikerank),
            rank_t2=_to_int(r.t2rank),
            rank_run=_to_int(r.runrank),
        )
    return out


def get_race_detail(
    event_id: int,
    prog_id: int,
    a_id: int,
    b_id: int,
    *,
    engine: Engine,
) -> RaceDetailBundle | None:
    """Top-level: assemble everything the race-detail page needs."""
    event = _fetch_event(event_id, prog_id, engine)
    if event is None:
        return None
    athletes = _fetch_athletes(event_id, prog_id, a_id, b_id, engine)
    conditions = _fetch_conditions(event_id, prog_id, engine)
    podium = _fetch_podium(event_id, prog_id, engine)
    return RaceDetailBundle(
        event=event,
        conditions=conditions,
        podium=podium,
        athlete_a=athletes.get(int(a_id)),
        athlete_b=athletes.get(int(b_id)),
    )
