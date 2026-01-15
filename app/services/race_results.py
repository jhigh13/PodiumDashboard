from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select, text

from app.data.db import get_session
from app.data.triathlon_db import get_triathlon_engine
from app.models.tables import Athlete, WTODashboardAthleteMap, WTORaceResult, Workout, DailyMetric


_FINISH_STATUS_ORDER = {
    # Higher = worse
    "FINISH": 0,
    "": 1,
    None: 1,
    "LAP": 2,
    "DNS": 3,
    "DNF": 4,
    "DSQ": 5,
}


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip().lower()
    # Strip accents/diacritics for more stable matching (e.g., "Gómez" -> "gomez").
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _name_tokens(value: str | None) -> list[str]:
    n = normalize_name(value)
    return [t for t in n.split() if t]


@dataclass(frozen=True)
class WTOMatchCandidate:
    athlete_id: int
    full_name: str
    score: int
    match_method: str


def _best_match_score(podium_name: str, wto_name: str) -> int:
    pn_tokens = _name_tokens(podium_name)
    wn_tokens = _name_tokens(wto_name)
    if not pn_tokens or not wn_tokens:
        return 0

    pn = " ".join(pn_tokens)
    wn = " ".join(wn_tokens)
    if pn == wn:
        return 100

    # Strong signals: matching last name and first name/initial.
    p_first, p_last = pn_tokens[0], pn_tokens[-1]
    w_first, w_last = wn_tokens[0], wn_tokens[-1]
    if p_last == w_last:
        if p_first == w_first:
            return 98
        if p_first and w_first and p_first[0] == w_first[0]:
            return 90
        # Same last name only: plausible but not definitive.
        return 75

    # Weaker signals: containment and token overlap.
    if pn in wn or wn in pn:
        return 80

    shared = set(pn_tokens).intersection(set(wn_tokens))
    if len(shared) >= 2:
        return 70
    if len(shared) == 1:
        return 40
    return 0


def _match_method_for_score(score: int) -> str:
    if score >= 98:
        return "strong"
    if score >= 90:
        return "last+initial"
    if score >= 75:
        return "last"
    if score >= 70:
        return "overlap"
    if score >= 40:
        return "weak"
    return "candidate"


def _choose_return_count(scored: list[tuple[int, WTOMatchCandidate]], limit: int) -> int:
    """Return at least `limit`, and include extra candidates if scores are close.

    This helps when common surnames yield multiple near-ties.
    """
    if not scored:
        return 0

    base = min(limit, len(scored))
    if base == len(scored):
        return base

    cutoff_score = scored[base - 1][0]
    # Include candidates within 3 points of the cutoff, up to a sane maximum.
    max_return = min(25, len(scored))
    extra = base
    while extra < max_return and scored[extra][0] >= (cutoff_score - 3):
        extra += 1
    return extra


_DIACRITIC_SRC = "áàäâãåçéèëêíìïîñóòöôõúùüûýÿ"
_DIACRITIC_DST = "aaaaaaceeeeiiiinooooouuuuyy"


def _sql_fold_name(expr: str) -> str:
    """Best-effort, built-in diacritic folding for Postgres without extensions.

    This is intentionally limited to common latin diacritics (e.g., ñ → n).
    """
    return f"translate(lower({expr}), '{_DIACRITIC_SRC}', '{_DIACRITIC_DST}')"


def find_wto_candidates(podium_name: str, limit: int = 10) -> List[WTOMatchCandidate]:
    engine = get_triathlon_engine()
    if engine is None:
        raise RuntimeError("TRIATHLON_DATABASE_URL is not set")

    # Pull a larger candidate pool first, then score locally.
    # This avoids missing the correct person when the surname is common.
    pn_tokens = _name_tokens(podium_name)
    if not pn_tokens:
        return []

    last = pn_tokens[-1]
    first = pn_tokens[0] if len(pn_tokens) >= 2 else None
    first_initial = first[0] if first else None

    # Two-stage query: strict (first+last) then loose (last only).
    folded_full_name = _sql_fold_name("full_name")
    strict_sql = text(
        """
        SELECT
            athlete_id,
            full_name
        FROM public.athlete
        WHERE {folded_full_name} LIKE :last_q
          AND {folded_full_name} LIKE :first_q
        ORDER BY athlete_id
        LIMIT :limit
        """
        .format(folded_full_name=folded_full_name)
    )
    loose_sql = text(
        """
        SELECT
            athlete_id,
            full_name
        FROM public.athlete
        WHERE {folded_full_name} LIKE :last_q
        ORDER BY athlete_id
        LIMIT :limit
        """
        .format(folded_full_name=folded_full_name)
    )

    max_fetch_strict = 200
    max_fetch_loose = 600
    rows: list[tuple[Any, Any]] = []
    seen: set[int] = set()
    with engine.connect() as conn:
        if first:
            strict_rows = conn.execute(
                strict_sql,
                {
                    "last_q": f"%{last}%",
                    "first_q": f"%{first}%",
                    "limit": max_fetch_strict,
                },
            ).fetchall()
            for athlete_id, full_name in strict_rows:
                aid = int(athlete_id)
                if aid in seen:
                    continue
                seen.add(aid)
                rows.append((athlete_id, full_name))

        loose_rows = conn.execute(
            loose_sql,
            {"last_q": f"%{last}%", "limit": max_fetch_loose},
        ).fetchall()
        for athlete_id, full_name in loose_rows:
            aid = int(athlete_id)
            if aid in seen:
                continue
            seen.add(aid)
            rows.append((athlete_id, full_name))

    scored: list[tuple[int, WTOMatchCandidate]] = []
    for athlete_id, full_name in rows:
        score = _best_match_score(podium_name, full_name)
        method = _match_method_for_score(score)
        scored.append((score, WTOMatchCandidate(int(athlete_id), str(full_name or ""), int(score), method)))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Keep only reasonably related candidates. If everything is weak, still show a few.
    filtered = [(s, c) for s, c in scored if s >= 40]
    if not filtered:
        filtered = scored

    take = _choose_return_count(filtered, max(1, int(limit)))
    return [c for _, c in filtered[:take]]


def get_mapped_podium_athlete_ids() -> list[int]:
    with get_session() as session:
        stmt = select(WTODashboardAthleteMap.podium_athlete_id).where(WTODashboardAthleteMap.wto_athlete_id.is_not(None))
        return [int(x) for x in session.execute(stmt).scalars().all()]


def fetch_wto_athlete_full_name(wto_athlete_id: int) -> str | None:
    engine = get_triathlon_engine()
    if engine is None:
        raise RuntimeError("TRIATHLON_DATABASE_URL is not set")

    sql = text(
        """
        SELECT
            full_name
        FROM public.athlete
        WHERE athlete_id = :athlete_id
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"athlete_id": int(wto_athlete_id)}).fetchone()
    if not row:
        return None
    return str(row[0]) if row[0] is not None else None


def get_or_create_mapping(podium_athlete_id: int) -> WTODashboardAthleteMap | None:
    with get_session() as session:
        stmt = select(WTODashboardAthleteMap).where(WTODashboardAthleteMap.podium_athlete_id == podium_athlete_id)
        return session.execute(stmt).scalars().first()


def upsert_mapping(
    podium_athlete_id: int,
    wto_athlete_id: int,
    wto_full_name: str,
    matched_method: str,
) -> WTODashboardAthleteMap:
    with get_session() as session:
        athlete = session.get(Athlete, podium_athlete_id)
        podium_name = athlete.name if athlete else None

        stmt = select(WTODashboardAthleteMap).where(WTODashboardAthleteMap.podium_athlete_id == podium_athlete_id)
        existing = session.execute(stmt).scalars().first()
        now = datetime.now(timezone.utc)
        if existing:
            existing.podium_name = podium_name
            existing.wto_athlete_id = wto_athlete_id
            existing.wto_full_name = wto_full_name
            existing.matched_method = matched_method
            existing.confirmed_at = now
            session.commit()
            return existing

        row = WTODashboardAthleteMap(
            podium_athlete_id=podium_athlete_id,
            podium_name=podium_name,
            wto_athlete_id=wto_athlete_id,
            wto_full_name=wto_full_name,
            matched_method=matched_method,
            confirmed_at=now,
        )
        session.add(row)
        session.commit()
        return row


def sync_race_results_last_two_years(podium_athlete_id: int) -> dict:
    mapping = get_or_create_mapping(podium_athlete_id)
    if not mapping or not mapping.wto_athlete_id:
        raise RuntimeError("No WTO mapping exists for this athlete yet")

    end_date = date.today()
    start_date = end_date - timedelta(days=365 * 2)

    engine = get_triathlon_engine()
    if engine is None:
        raise RuntimeError("TRIATHLON_DATABASE_URL is not set")

    sql = text(
        """
        SELECT
            rr.event_id,
            rr.prog_id,
            rr.athlete_id,
            rr.athlete_full_name,
            rr.finish_status,
            rr.finish_position,
            rr.position_sort,
            rr.total_time,
            rr.swimtime,
            rr.t1time,
            rr.biketime,
            rr.t2time,
            rr.runtime,
            ev.event_date,
            ev.event_name,
            ev.prog_name,
            ev.prog_distance_category
        FROM public.race_results rr
        JOIN public.events ev
            ON ev.event_id = rr.event_id
           AND ev.prog_id = rr.prog_id
        WHERE rr.athlete_id = :athlete_id
          AND ev.event_date >= :start_date
          AND ev.event_date <= :end_date
        ORDER BY ev.event_date DESC
        """
    )

    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "athlete_id": int(mapping.wto_athlete_id),
                "start_date": start_date,
                "end_date": end_date,
            },
        ).fetchall()

    inserted = 0
    with get_session() as session:
        # Clear existing cached results in that window for this athlete
        session.execute(
            delete(WTORaceResult)
            .where(WTORaceResult.podium_athlete_id == podium_athlete_id)
            .where(WTORaceResult.event_date >= start_date)
            .where(WTORaceResult.event_date <= end_date)
        )

        for r in rows:
            (
                event_id,
                prog_id,
                athlete_id,
                athlete_full_name,
                finish_status,
                finish_position,
                position_sort,
                total_time,
                swimtime,
                t1time,
                biketime,
                t2time,
                runtime,
                event_date,
                event_name,
                prog_name,
                prog_distance_category,
            ) = r

            record = WTORaceResult(
                podium_athlete_id=podium_athlete_id,
                wto_athlete_id=int(athlete_id) if athlete_id is not None else None,
                event_id=int(event_id) if event_id is not None else None,
                prog_id=int(prog_id) if prog_id is not None else None,
                event_date=event_date,
                event_name=event_name,
                prog_name=prog_name,
                prog_distance_category=prog_distance_category,
                finish_status=finish_status,
                finish_position=finish_position,
                position_sort=position_sort,
                total_time=total_time,
                swim_time=swimtime,
                t1_time=t1time,
                bike_time=biketime,
                t2_time=t2time,
                run_time=runtime,
                raw_json={
                    "athlete_full_name": athlete_full_name,
                },
            )
            session.add(record)
            inserted += 1

        session.commit()

    return {
        "podium_athlete_id": podium_athlete_id,
        "wto_athlete_id": mapping.wto_athlete_id,
        "range": f"{start_date.isoformat()}..{end_date.isoformat()}",
        "rows": len(rows),
        "inserted": inserted,
    }


def load_local_race_results(podium_athlete_id: int) -> List[Dict[str, Any]]:
    with get_session() as session:
        stmt = (
            select(WTORaceResult)
            .where(WTORaceResult.podium_athlete_id == podium_athlete_id)
            .order_by(WTORaceResult.event_date.desc())
        )
        rows = session.execute(stmt).scalars().all()

    out = []
    for row in rows:
        out.append(
            {
                "event_date": row.event_date,
                "event_name": row.event_name,
                "prog_name": row.prog_name,
                "distance": row.prog_distance_category,
                "finish_status": row.finish_status,
                "finish_position": row.finish_position,
                "total_time": row.total_time,
                "event_id": row.event_id,
                "prog_id": row.prog_id,
            }
        )
    return out


def _status_worstness(status: Optional[str]) -> int:
    return _FINISH_STATUS_ORDER.get(status, 1)


def pick_best_worst(races: List[Dict[str, Any]]) -> dict:
    finished = [r for r in races if (r.get("finish_status") == "FINISH") and isinstance(r.get("finish_position"), int)]
    best_finished = min(finished, key=lambda r: r["finish_position"], default=None)
    worst_finished = max(finished, key=lambda r: r["finish_position"], default=None)

    # Worst including non-finishes: pick the worst status first, then worst position
    def sort_key(r: Dict[str, Any]):
        status = r.get("finish_status")
        pos = r.get("finish_position")
        # Higher status severity is worse; within FINISH, larger position is worse
        return (_status_worstness(status), pos if isinstance(pos, int) else -1)

    worst_including = max(races, key=sort_key, default=None)

    return {
        "best_finished": best_finished,
        "worst_finished": worst_finished,
        "worst_including": worst_including,
    }


def compute_pre_race_training_summary(podium_athlete_id: int, race_date: date, lookback_days: int = 28) -> Dict[str, Any]:
    start = race_date - timedelta(days=lookback_days)
    end = race_date - timedelta(days=1)

    with get_session() as session:
        workouts = session.execute(
            select(Workout)
            .where(Workout.athlete_id == podium_athlete_id)
            .where(Workout.date >= start)
            .where(Workout.date <= end)
        ).scalars().all()

        metrics = session.execute(
            select(DailyMetric)
            .where(DailyMetric.athlete_id == podium_athlete_id)
            .where(DailyMetric.date >= start)
            .where(DailyMetric.date <= end)
        ).scalars().all()

    tss_vals = [w.tss for w in workouts if isinstance(w.tss, (int, float))]
    total_tss = float(sum(tss_vals)) if tss_vals else None

    hrv_vals = [m.hrv for m in metrics if isinstance(m.hrv, (int, float))]
    sleep_vals = [m.sleep_hours for m in metrics if isinstance(m.sleep_hours, (int, float))]

    def avg(xs: List[float]) -> Optional[float]:
        return float(sum(xs) / len(xs)) if xs else None

    return {
        "window": f"{start.isoformat()}..{end.isoformat()}",
        "workouts": len(workouts),
        "total_tss": total_tss,
        "avg_hrv": avg([float(x) for x in hrv_vals]),
        "avg_sleep_hours": avg([float(x) for x in sleep_vals]),
    }
