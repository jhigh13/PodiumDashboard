from datetime import date, timedelta, datetime
from sqlalchemy import select, delete
from app.data.db import get_session
from app.models.tables import Workout, DailyMetric
from app.services.tp_api import get_api
from app.services.tokens import get_token as _get_token, find_coach_token as _find_coach_token
from app.services.athletes import get_or_create_demo_athlete, get_athlete_by_id
from app.services.baseline import calculate_baselines
from app.services.recovery_alerts import evaluate_recovery_alert
from app.utils.dates import get_effective_today
from app.services.compliance import upsert_workout_compliance, get_compliance_for_day


def _coerce_date(value):
    """Best-effort convert API date fields to date objects."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    # assume string
    try:
        # trim time part if present
        if 'T' in value:
            value = value.split('T')[0]
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def ingest_recent(days: int = 7, athlete_id: int | None = None):
    athlete = get_athlete_by_id(athlete_id) if athlete_id else get_or_create_demo_athlete()
    api = get_api(athlete.id)
    days = max(days, 1)
    end = get_effective_today()
    start = end - timedelta(days=days - 1)
    tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
    # If we're using a coach token and no tp_athlete_id is set, we cannot fetch
    if not _get_token(athlete.id) and _find_coach_token() and not tp_athlete_id:
        raise RuntimeError("Selected athlete has no TrainingPeaks ID yet. Fetch roster first or set tp_athlete_id.")

    workouts = api.fetch_workouts(start, end, tp_athlete_id=tp_athlete_id)
    workouts_fetched = len(workouts)
    first_workout_keys = list(workouts[0].keys()) if workouts else []
    first_workout_sample = None
    stored = 0
    duplicates = 0
    sample_workout_ids = []
    plan_cache: dict[str, dict | None] = {}
    compliance_updates: list[dict[str, object]] = []

    with get_session() as session:
        for idx, w in enumerate(workouts):
            if idx == 0:
                # store a trimmed sample (avoid huge raw)
                first_workout_sample = {k: w[k] for k in list(w.keys())[:12]}
            # attempt broader id detection
            wid_candidate = (
                w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
            )
            if not wid_candidate:
                # search any key ending with 'Id' or 'ID'
                for k, v in w.items():
                    if k.lower().endswith('id') and v:
                        wid_candidate = v
                        break
            workout_id = str(wid_candidate or '')
            if workout_id:
                if len(sample_workout_ids) < 5:
                    sample_workout_ids.append(workout_id)
            if not workout_id:
                continue
            # Check existing
            stmt = select(Workout).where(Workout.tp_workout_id == workout_id)
            existing_record = session.execute(stmt).scalars().first()
            is_new_record = existing_record is None

            if is_new_record:
                # Duration: prefer TotalTime (seconds?) else TotalTimePlanned; if looks like hours convert
                raw_total = w.get('TotalTime') or w.get('TotalTimePlanned') or w.get('TotalTimePlannedSeconds')
                duration_sec = 0
                if raw_total is not None:
                    try:
                        val = float(raw_total)
                        # Heuristic: if val < 20 assume hours, else assume seconds (many APIs use seconds; adjust if wrong later)
                        duration_sec = int(val * 3600) if val < 20 else int(val)
                    except Exception:  # noqa: BLE001
                        duration_sec = 0
                tss_val = w.get('tss') or w.get('TssActual') or w.get('TSSActual') or w.get('TssPlanned')
                if_val = w.get('intensityFactor') or w.get('IF') or w.get('If')
                date_field = w.get('workoutDay') or w.get('WorkoutDay') or w.get('Date')
                record = Workout(
                    athlete_id=athlete.id,
                    tp_workout_id=workout_id,
                    date=_coerce_date(date_field),
                    sport=w.get('sportType') or w.get('sport') or w.get('WorkoutType'),
                    duration_sec=duration_sec,
                    tss=tss_val,
                    intensity_factor=if_val,
                    raw_json=w,
                )
                session.add(record)
                session.flush()  # ensure record.id populated for compliance linkage
                stored += 1
            else:
                duplicates += 1
                record = existing_record
                # Update raw payload for existing entries so compliance has latest data
                record.raw_json = w or record.raw_json

            plan_data = None
            if workout_id:
                if workout_id not in plan_cache:
                    try:
                        plan_cache[workout_id] = api.fetch_workout_details(workout_id, tp_athlete_id=tp_athlete_id)
                    except Exception:  # noqa: BLE001
                        plan_cache[workout_id] = None
                plan_data = plan_cache[workout_id]

            compliance_summary = upsert_workout_compliance(session, record, plan_data)
            if compliance_summary:
                compliance_updates.append({
                    "workout_id": workout_id,
                    "sport": record.sport,
                    "date": record.date.isoformat() if record.date else None,
                    "overall_score": compliance_summary.get("overall_score"),
                    "notes": compliance_summary.get("notes"),
                })
        session.commit()

    # Persist tp_athlete_id if missing but present in workouts
    if tp_athlete_id is None:
        inferred_id = None
        for w in workouts:
            if w.get('AthleteId'):
                inferred_id = w.get('AthleteId')
                break
        if inferred_id:
            with get_session() as session:
                # Update athlete with inferred tp_athlete_id from workout data
                from sqlalchemy import text
                session.execute(
                    text("UPDATE athletes SET tp_athlete_id = :aid WHERE id = :id"),
                    {"aid": inferred_id, "id": athlete.id},
                )
                session.commit()
            tp_athlete_id = inferred_id

    # Metrics range (same period) - we will store ALL metrics for the date range
    metrics = api.fetch_daily_metrics_range(start, end, tp_athlete_id=tp_athlete_id)
    metrics_fetched = len(metrics) if metrics else 0
    metrics_saved = 0
    metrics_raw_sample = []
    metric_field_names = set()
    metrics_dates_saved = []  # Track which dates we saved
    
    if metrics:
        # Collect sample and all field names to help debug
        if len(metrics) > 0:
            metrics_raw_sample = [metrics[0]]  # Keep first metric for debugging
            for m in metrics[:5]:  # Check first 5 for field names
                metric_field_names.update(m.keys())
        
        # Process each metric entry (API returns array, each can have different fields)
        for m in metrics:
            # Parse date from DateTime field (format: "2022-06-01T06:12:34")
            date_str = m.get('DateTime') or m.get('datetime') or m.get('Date')
            if not date_str:
                continue
            metric_date = _coerce_date(date_str)
            if not metric_date:
                continue
            
            # Map TrainingPeaks API fields to our DB columns
            # Per API docs: Pulse, HRV, SleepHours, SleepQuality, WeightInKilograms, Steps, Stress
            # Plus: ctl, atl, tsb (fitness metrics)
            with get_session() as session:
                # Delete existing metric for this athlete/date to avoid duplicates
                session.execute(delete(DailyMetric).where(
                    DailyMetric.athlete_id == athlete.id,
                    DailyMetric.date == metric_date
                ))
                
                dm = DailyMetric(
                    athlete_id=athlete.id,
                    date=metric_date,
                    rhr=m.get('Pulse') or m.get('RestingHeartRate') or m.get('restingHeartRate'),
                    hrv=m.get('Hrv') or m.get('HRV') or m.get('hrv'),  # Fixed: API uses 'Hrv' (PascalCase)
                    sleep_hours=m.get('SleepHours') or m.get('sleepHours'),
                    body_score=m.get('BodyScore') or m.get('bodyScore'),
                    ctl=m.get('CTL') or m.get('ctl') or m.get('Ctl'),
                    atl=m.get('ATL') or m.get('atl') or m.get('Atl'),
                    tsb=m.get('TSB') or m.get('tsb') or m.get('Tsb'),
                )
                session.add(dm)
                session.commit()
                metrics_saved += 1
                metrics_dates_saved.append(metric_date.isoformat())

    baseline_summary = calculate_baselines(athlete.id, end_date=end)
    alert_result = evaluate_recovery_alert(athlete.id, check_date=end)
    latest_compliance = get_compliance_for_day(athlete.id, end)

    return {
        "tp_athlete_id": tp_athlete_id,
        "used_coach_token": bool(_find_coach_token() and not _get_token(athlete.id)),
        "range": f"{start.isoformat()}..{end.isoformat()}",
        "range_days": days,
        "workouts_fetched": workouts_fetched,
        "workouts_inserted": stored,
        "workout_duplicates": duplicates,
        "sample_workout_ids": sample_workout_ids,
        "first_workout_keys": first_workout_keys,
        "first_workout_sample": first_workout_sample,
        "metrics_fetched": metrics_fetched,
        "metrics_saved": metrics_saved,
        "metrics_dates_saved": sorted(metrics_dates_saved),  # Show which specific dates were saved
        "metrics_raw_sample": metrics_raw_sample,
        "metric_field_names": sorted(list(metric_field_names)),
        "baseline_summary": baseline_summary,
        "recovery_alert": alert_result,
        "compliance_updates": compliance_updates,
    "latest_compliance": latest_compliance,
        "note": "Duration heuristic: <20 treated as hours else seconds; Metrics: check field_names for API structure"
    }


def ingest_historical_full(days_back: int = 365, athlete_id: int | None = None, segments: int = 9):
    """Fetch full historical range in one call (no chunking) and save in bulk.

    Faster for coach workflows when API endpoints can handle the full range.
    Set segments>1 (default=9) to force splitting the date range into equal
    parts. Default of 9 segments keeps each chunk within TrainingPeaks' 45-day
    maximum (365÷9≈40 days per segment).
    """
    athlete = get_athlete_by_id(athlete_id) if athlete_id else get_or_create_demo_athlete()
    api = get_api(athlete.id)
    end_date = get_effective_today()
    start_date = end_date - timedelta(days=days_back)
    tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
    if not _get_token(athlete.id) and _find_coach_token() and not tp_athlete_id:
        raise RuntimeError("Selected athlete has no TrainingPeaks ID yet. Fetch roster first or set tp_athlete_id.")

    # Helper to store workouts
    def _store_workouts(items):
        nonlocal stored_w, dup_w
        with get_session() as session:
            for w in items:
                wid_candidate = (
                    w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
                )
                if not wid_candidate:
                    for k, v in w.items():
                        if k.lower().endswith('id') and v:
                            wid_candidate = v
                            break
                workout_id = str(wid_candidate or '')
                if not workout_id:
                    continue
                # Check existing
                stmt = select(Workout).where(Workout.tp_workout_id == workout_id)
                if session.execute(stmt).scalars().first():
                    dup_w += 1
                    continue
                raw_total = w.get('TotalTime') or w.get('TotalTimePlanned') or w.get('TotalTimePlannedSeconds')
                duration_sec = 0
                if raw_total is not None:
                    try:
                        val = float(raw_total)
                        duration_sec = int(val * 3600) if val < 20 else int(val)
                    except Exception:  # noqa: BLE001
                        duration_sec = 0
                tss_val = w.get('tss') or w.get('TssActual') or w.get('TSSActual') or w.get('TssPlanned')
                if_val = w.get('intensityFactor') or w.get('IF') or w.get('If')
                date_field = w.get('workoutDay') or w.get('WorkoutDay') or w.get('Date')
                record = Workout(
                    athlete_id=athlete.id,
                    tp_workout_id=workout_id,
                    date=_coerce_date(date_field),
                    sport=w.get('sportType') or w.get('sport') or w.get('WorkoutType'),
                    duration_sec=duration_sec,
                    tss=tss_val,
                    intensity_factor=if_val,
                    raw_json=w,
                )
                session.add(record)
                stored_w += 1
            session.commit()

    # Helper to store metrics
    def _store_metrics(items):
        nonlocal saved_m
        if not items:
            return
        with get_session() as session:
            for m in items:
                date_str = m.get('DateTime') or m.get('datetime') or m.get('Date')
                metric_date = _coerce_date(date_str)
                if not metric_date:
                    continue
                session.execute(delete(DailyMetric).where(
                    DailyMetric.athlete_id == athlete.id,
                    DailyMetric.date == metric_date
                ))
                dm = DailyMetric(
                    athlete_id=athlete.id,
                    date=metric_date,
                    rhr=m.get('Pulse') or m.get('RestingHeartRate') or m.get('restingHeartRate'),
                    hrv=m.get('Hrv') or m.get('HRV') or m.get('hrv'),
                    sleep_hours=m.get('SleepHours') or m.get('sleepHours'),
                    body_score=m.get('BodyScore') or m.get('bodyScore'),
                    ctl=m.get('CTL') or m.get('ctl') or m.get('Ctl'),
                    atl=m.get('ATL') or m.get('atl') or m.get('Atl'),
                    tsb=m.get('TSB') or m.get('tsb') or m.get('Tsb'),
                )
                session.add(dm)
                saved_m += 1
            session.commit()

    stored_w = 0
    dup_w = 0
    saved_m = 0
    failed_segments = []

    # Partition into segments if requested
    segments = max(1, int(segments))
    total_days = (end_date - start_date).days + 1
    seg_days = (total_days + segments - 1) // segments  # ceil division
    ranges = []
    for i in range(segments):
        seg_start = start_date + timedelta(days=i * seg_days)
        if seg_start > end_date:
            break
        seg_end = min(end_date, seg_start + timedelta(days=seg_days - 1))
        ranges.append((seg_start, seg_end))

    for seg_start, seg_end in ranges:
        try:
            w_items = api.fetch_workouts(seg_start, seg_end, tp_athlete_id=tp_athlete_id)
            _store_workouts(w_items)
        except Exception as e:  # noqa: BLE001
            failed_segments.append({"type": "workouts", "range": f"{seg_start}..{seg_end}", "error": str(e)})
        try:
            m_items = api.fetch_daily_metrics_range(seg_start, seg_end, tp_athlete_id=tp_athlete_id)
            _store_metrics(m_items)
        except Exception as e:  # noqa: BLE001
            failed_segments.append({"type": "metrics", "range": f"{seg_start}..{seg_end}", "error": str(e)})

    return {
        "tp_athlete_id": tp_athlete_id,
        "used_coach_token": bool(_find_coach_token() and not _get_token(athlete.id)),
        "date_range": f"{start_date}..{end_date}",
        "segments": len(ranges),
        "workouts_inserted": stored_w,
        "workout_duplicates": dup_w,
        "metrics_saved": saved_m,
        "failed_segments": failed_segments,
    }


def ingest_historical(days_back: int = 365, chunk_size: int = 30, athlete_id: int | None = None):
    """Fetch historical metrics and workouts in chunks to establish baseline.
    
    Args:
        days_back: How many days back to fetch (default 365 for 1 year)
        chunk_size: Size of each date range chunk to avoid API timeouts (default 30 days)
    
    Returns:
        dict with summary of all chunks processed
    """
    athlete = get_athlete_by_id(athlete_id) if athlete_id else get_or_create_demo_athlete()
    api = get_api(athlete.id)
    tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
    
    end_date = get_effective_today()
    start_date = end_date - timedelta(days=days_back)
    
    total_workouts = 0
    total_metrics = 0
    total_workout_duplicates = 0
    chunks_processed = 0
    chunks_total = (days_back + chunk_size - 1) // chunk_size  # Ceiling division
    failed_chunks = []
    
    # Process in reverse chronological order (most recent first)
    current_end = end_date
    
    while current_end > start_date:
        current_start = max(current_end - timedelta(days=chunk_size), start_date)
        chunks_processed += 1
        
        try:
            # Fetch workouts for this chunk
            workouts = api.fetch_workouts(current_start, current_end, tp_athlete_id=tp_athlete_id)
            workout_count = 0
            duplicate_count = 0
            
            with get_session() as session:
                for w in workouts:
                    wid_candidate = (
                        w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
                    )
                    if not wid_candidate:
                        for k, v in w.items():
                            if k.lower().endswith('id') and v:
                                wid_candidate = v
                                break
                    workout_id = str(wid_candidate or '')
                    if not workout_id:
                        continue
                    
                    # Check existing
                    stmt = select(Workout).where(Workout.tp_workout_id == workout_id)
                    if session.execute(stmt).scalars().first():
                        duplicate_count += 1
                        continue
                    
                    raw_total = w.get('TotalTime') or w.get('TotalTimePlanned') or w.get('TotalTimePlannedSeconds')
                    duration_sec = 0
                    if raw_total is not None:
                        try:
                            val = float(raw_total)
                            duration_sec = int(val * 3600) if val < 20 else int(val)
                        except Exception:  # noqa: BLE001
                            duration_sec = 0
                    
                    tss_val = w.get('tss') or w.get('TssActual') or w.get('TSSActual') or w.get('TssPlanned')
                    if_val = w.get('intensityFactor') or w.get('IF') or w.get('If')
                    date_field = w.get('workoutDay') or w.get('WorkoutDay') or w.get('Date')
                    
                    record = Workout(
                        athlete_id=athlete.id,
                        tp_workout_id=workout_id,
                        date=_coerce_date(date_field),
                        sport=w.get('sportType') or w.get('sport') or w.get('WorkoutType'),
                        duration_sec=duration_sec,
                        tss=tss_val,
                        intensity_factor=if_val,
                        raw_json=w,
                    )
                    session.add(record)
                    workout_count += 1
                session.commit()
            
            total_workouts += workout_count
            total_workout_duplicates += duplicate_count
            
            # Fetch metrics for this chunk
            metrics = api.fetch_daily_metrics_range(current_start, current_end, tp_athlete_id=tp_athlete_id)
            metric_count = 0
            
            if metrics:
                for m in metrics:
                    date_str = m.get('DateTime') or m.get('datetime') or m.get('Date')
                    if not date_str:
                        continue
                    metric_date = _coerce_date(date_str)
                    if not metric_date:
                        continue
                    
                    with get_session() as session:
                        # Delete existing to avoid duplicates
                        session.execute(delete(DailyMetric).where(
                            DailyMetric.athlete_id == athlete.id,
                            DailyMetric.date == metric_date
                        ))
                        
                        dm = DailyMetric(
                            athlete_id=athlete.id,
                            date=metric_date,
                            rhr=m.get('Pulse') or m.get('RestingHeartRate') or m.get('restingHeartRate'),
                            hrv=m.get('Hrv') or m.get('HRV') or m.get('hrv'),  # Fixed: API uses 'Hrv' (PascalCase)
                            sleep_hours=m.get('SleepHours') or m.get('sleepHours'),
                            body_score=m.get('BodyScore') or m.get('bodyScore'),
                            ctl=m.get('CTL') or m.get('ctl') or m.get('Ctl'),
                            atl=m.get('ATL') or m.get('atl') or m.get('Atl'),
                            tsb=m.get('TSB') or m.get('tsb') or m.get('Tsb'),
                        )
                        session.add(dm)
                        session.commit()
                        metric_count += 1
            
            total_metrics += metric_count
            
        except Exception as e:  # noqa: BLE001
            failed_chunks.append({
                "range": f"{current_start} to {current_end}",
                "error": str(e)
            })
        
        # Move to next chunk
        current_end = current_start - timedelta(days=1)
        
        # Yield progress for UI updates
        yield {
            "status": "in_progress",
            "chunks_processed": chunks_processed,
            "chunks_total": chunks_total,
            "progress_percent": int((chunks_processed / chunks_total) * 100),
            "current_range": f"{current_start} to {current_end}",
            "workouts_so_far": total_workouts,
            "metrics_so_far": total_metrics,
        }
    
    # Final summary
    yield {
        "status": "complete",
        "chunks_processed": chunks_processed,
        "chunks_total": chunks_total,
        "progress_percent": 100,
        "date_range": f"{start_date} to {end_date}",
        "total_workouts_inserted": total_workouts,
        "total_workout_duplicates": total_workout_duplicates,
        "total_metrics_inserted": total_metrics,
        "failed_chunks": failed_chunks,
        "tp_athlete_id": tp_athlete_id,
    }
