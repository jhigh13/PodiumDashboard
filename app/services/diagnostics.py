from datetime import date, timedelta
from typing import List, Dict, Any
from app.services.tp_api import get_api
from app.services.athletes import get_or_create_demo_athlete


def fetch_raw_metrics(start: date, end: date) -> List[Dict[str, Any]]:
    api = get_api()
    athlete = get_or_create_demo_athlete()
    tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
    try:
        return api.fetch_daily_metrics_range(start, end, tp_athlete_id=tp_athlete_id)
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e), "start": str(start), "end": str(end)}]


def find_missing_dates(days_back: int = 30) -> Dict[str, Any]:
    """Compare expected continuous date range vs dates present in DB metrics.
    Returns dict with gaps and raw API check for those gaps.
    """
    from app.data.db import get_session
    from app.models.tables import DailyMetric
    from sqlalchemy import select

    end = date.today()
    start = end - timedelta(days=days_back - 1)

    with get_session() as session:
        stmt = select(DailyMetric).where(
            DailyMetric.date >= start,
            DailyMetric.date <= end
        ).order_by(DailyMetric.date)
        rows = session.execute(stmt).scalars().all()

    present_dates = {r.date for r in rows}
    expected_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    missing = [d for d in expected_dates if d not in present_dates]

    api_raw_missing = []
    if missing:
        # Group consecutive missing dates into ranges
        ranges = []
        cur_start = missing[0]
        prev = missing[0]
        for d in missing[1:]:
            if d == prev + timedelta(days=1):
                prev = d
            else:
                ranges.append((cur_start, prev))
                cur_start = d
                prev = d
        ranges.append((cur_start, prev))

        # Fetch raw metrics for each missing range to validate if API returns anything
        for r_start, r_end in ranges:
            raw = fetch_raw_metrics(r_start, r_end)
            error = None
            if raw and isinstance(raw, list) and "error" in raw[0]:
                error = raw[0]["error"]
            api_raw_missing.append({
                "range": f"{r_start}..{r_end}",
                "count": 0 if error else len(raw),
                "sample": [] if error else raw[:2],
                "error": error,
            })

    return {
        "start": start,
        "end": end,
        "expected_days": len(expected_dates),
        "present_days": len(present_dates),
        "missing_count": len(missing),
        "missing_dates": missing,
        "api_probe": api_raw_missing,
    }
