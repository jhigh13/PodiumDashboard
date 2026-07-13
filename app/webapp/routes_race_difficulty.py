"""Race Difficulty coach-tab routes.

Wire-up: import and call ``register_race_difficulty_routes(app, templates,
require_coach=require_coach)`` from inside ``app/webapp/app.py::create_app()``.
``require_coach`` is injected because app.py imports this module before the
dependency is defined (circular import otherwise).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.data.db import get_session
from app.data.triathlon_db import get_triathlon_engine
from app.models.tables import Athlete, CoachRosterMember, Workout, WTODashboardAthleteMap
from app.services.race_difficulty import (
    build_athlete_stream,
    derive_weight_kg,
    fetch_event_header,
    fetch_union_races,
    parse_event_prog_key,
    pick_default_workout,
    sport_matches,
)
from app.services.tp_api import get_api
from app.services.workout_cache import (
    fetch_timeseries_cached,
    get_cached_timeseries,
    is_timeseries_cached,
)

# Series palette shared by the line chart and radar (assigned server-side so
# both views agree on athlete colors). Fixed slot order is CVD-validated
# (dataviz reference palette, light mode); slots are assigned in order, never
# cycled — athletes beyond the 8 slots fall back to muted gray.
PALETTE = [
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
]
PALETTE_OVERFLOW = "#898781"

MAX_FANOUT_WORKERS = 6


def _error_card(msg: str) -> str:
    return f'<div class="card"><p class="muted">{msg}</p></div>'


def _coerce_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value)
    if "T" in s:
        s = s.split("T", 1)[0]
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def _format_duration(seconds: int | None) -> str:
    if not isinstance(seconds, int) or seconds <= 0:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_weight(value: str | None) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        w = float(s)
    except ValueError:
        return None
    return w if w > 0 else None


def register_race_difficulty_routes(app: FastAPI, templates: Jinja2Templates, *, require_coach) -> None:

    def _selected_athletes(coach_id: int, athlete_ids: list[int]) -> list[dict]:
        """Selected roster athletes as plain dicts (safe across threads),
        restricted to the coach's roster."""
        wanted = {int(a) for a in athlete_ids}
        if not wanted:
            return []
        with get_session() as s:
            rows = s.execute(
                select(Athlete)
                .join(CoachRosterMember, CoachRosterMember.athlete_id == Athlete.id)
                .where(CoachRosterMember.coach_athlete_id == int(coach_id))
                .where(Athlete.id.in_(wanted))
                .order_by(Athlete.name)
            ).scalars().all()
            return [
                {"id": int(a.id), "name": a.name or f"Athlete {a.id}", "tp_athlete_id": a.tp_athlete_id}
                for a in rows
            ]

    def _mappings_for(athletes: list[dict]) -> tuple[list[dict], list[str]]:
        """(mappings for fetch_union_races, names of unmapped athletes)."""
        ids = [a["id"] for a in athletes]
        with get_session() as s:
            rows = s.execute(
                select(WTODashboardAthleteMap)
                .where(WTODashboardAthleteMap.podium_athlete_id.in_(ids))
                .where(WTODashboardAthleteMap.wto_athlete_id.is_not(None))
            ).scalars().all()
            by_podium = {int(m.podium_athlete_id): int(m.wto_athlete_id) for m in rows}
        mappings = []
        unmapped = []
        for a in athletes:
            wto_id = by_podium.get(a["id"])
            if wto_id:
                mappings.append({"podium_athlete_id": a["id"], "name": a["name"], "wto_athlete_id": wto_id})
            else:
                unmapped.append(a["name"])
        return mappings, unmapped

    # ── Step 1: unique races for the selected athletes ────────────────────────

    @app.get("/partials/race_difficulty/races", response_class=HTMLResponse)
    def rd_races(
        request: Request,
        rd_year: int | None = Query(default=None),
        athlete_ids: list[int] = Query(default=[]),
        coach_id: int = Depends(require_coach),
    ):
        athletes = _selected_athletes(coach_id, athlete_ids)
        if not athletes:
            return HTMLResponse(_error_card("Select at least one athlete, then click Find Races."))

        engine = get_triathlon_engine()
        if engine is None:
            return HTMLResponse(_error_card("TRIATHLON_DATABASE_URL is not set — race lookup unavailable."))

        mappings, unmapped = _mappings_for(athletes)
        warnings = [
            f"{name} has no World Triathlon mapping yet — their races can't be listed. "
            f"Map them via Race Performance → Sync WTO Race Results."
            for name in unmapped
        ]
        if not mappings:
            return HTMLResponse(_error_card(
                "None of the selected athletes are mapped to World Triathlon athletes yet. "
                "Run a race-results sync on the Race Performance tab first."
            ))

        year = int(rd_year) if rd_year else date.today().year
        try:
            races = fetch_union_races(mappings, year, engine)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(_error_card(f"Could not load races from triathlon-db: {e}"))

        return templates.TemplateResponse(
            "partials/race_difficulty_races.html",
            {
                "request": request,
                "races": races,
                "warnings": warnings,
                "year": year,
                "n_selected": len(athletes),
            },
        )

    # ── Step 2: per-athlete bike workout picker (±1 day) + weight prefill ─────

    def _workouts_for_athlete(athlete: dict, event_date: date) -> dict:
        """One athlete's race-day workout options, default pick, and weight."""
        start = event_date - timedelta(days=1)
        end = event_date + timedelta(days=1)

        with get_session() as s:
            manual_rows = s.execute(
                select(Workout)
                .where(Workout.athlete_id == athlete["id"])
                .where(Workout.tp_workout_id.like("manual_%"))
                .where(Workout.date >= start)
                .where(Workout.date <= end)
                .order_by(Workout.date.desc())
            ).scalars().all()
            manual = []
            for w in manual_rows:
                raw = w.raw_json or {}
                dur = _format_duration(int(w.duration_sec)) if w.duration_sec else ""
                label_parts = [f"📎 {w.date}", str(w.sport or ""), str(raw.get("label") or "Uploaded FIT"), dur]
                manual.append({
                    "workout_id": str(w.tp_workout_id),
                    "sport": str(w.sport or ""),
                    "duration_sec": w.duration_sec,
                    "label": " • ".join(p for p in label_parts if p),
                })

        workouts = list(manual)
        warning = None
        has_tp = bool(athlete["tp_athlete_id"])
        if has_tp:
            try:
                api = get_api(athlete["id"])
                raw_workouts = api.fetch_workouts(start, end, tp_athlete_id=int(athlete["tp_athlete_id"]))
                for w in raw_workouts or []:
                    wid = w.get("workoutId") or w.get("WorkoutId") or w.get("id") or w.get("Id")
                    if not wid:
                        continue
                    sport = w.get("WorkoutType") or w.get("sportType") or w.get("sport") or ""
                    title = w.get("Title") or w.get("title") or w.get("WorkoutName") or ""
                    w_day = _coerce_date(w.get("workoutDay") or w.get("WorkoutDay") or w.get("Date") or w.get("date"))
                    completed = bool(w.get("Completed", False))
                    dur_sec = None
                    if completed and w.get("TotalTime"):
                        try:
                            val = float(w.get("TotalTime"))
                            dur_sec = int(val * 3600) if val < 20 else int(val)
                        except Exception:
                            dur_sec = None
                    parts = [str(p) for p in (w_day, sport, title) if p]
                    label = " • ".join(parts)
                    dur = _format_duration(dur_sec) if dur_sec else ""
                    if dur:
                        label = f"{label} — {dur}"
                    workouts.append({
                        "workout_id": str(wid),
                        "sport": str(sport or ""),
                        "duration_sec": dur_sec,
                        "label": label,
                    })
            except Exception as e:  # noqa: BLE001
                warning = f"TrainingPeaks fetch failed: {e}"
        elif not manual:
            warning = "No TrainingPeaks link and no manual uploads for race day."

        workouts.sort(
            key=lambda x: (
                0 if sport_matches(x.get("sport"), "bike") else 1,
                -(int(x.get("duration_sec") or 0)),
                x.get("label") or "",
            )
        )
        default_id = pick_default_workout(workouts, "bike")
        if not workouts:
            warning = warning or "No bike file found within ±1 day of the race."

        return {
            "athlete_id": athlete["id"],
            "name": athlete["name"],
            "has_tp": has_tp,
            "workouts": workouts,
            "default_workout_id": default_id,
            "weight_kg": derive_weight_kg(athlete["id"]),
            "warning": warning,
        }

    def _precache(athlete: dict, workout_id: str) -> None:
        if not workout_id or workout_id.startswith("manual_") or is_timeseries_cached(workout_id):
            return
        if not athlete["tp_athlete_id"]:
            return
        try:
            api = get_api(athlete["id"])
            fetch_timeseries_cached(api, workout_id, int(athlete["tp_athlete_id"]))
        except Exception:
            pass  # Non-critical; the analysis route will retry

    @app.get("/partials/race_difficulty/workouts", response_class=HTMLResponse)
    def rd_workouts(
        request: Request,
        race_key: str | None = Query(default=None),
        athlete_ids: list[int] = Query(default=[]),
        coach_id: int = Depends(require_coach),
    ):
        key = parse_event_prog_key(race_key)
        if not key:
            return HTMLResponse('<div class="muted">Pick a race above to choose each athlete\'s bike file.</div>')

        athletes = _selected_athletes(coach_id, athlete_ids)
        if not athletes:
            return HTMLResponse(_error_card("No selected athletes found in your roster."))

        engine = get_triathlon_engine()
        if engine is None:
            return HTMLResponse(_error_card("TRIATHLON_DATABASE_URL is not set."))
        header = fetch_event_header(key[0], key[1], engine)
        if not header or not header.get("event_date"):
            return HTMLResponse(_error_card("Race not found in triathlon-db."))
        event_date = header["event_date"]

        with ThreadPoolExecutor(max_workers=min(len(athletes), MAX_FANOUT_WORKERS)) as pool:
            rows = list(pool.map(lambda a: _workouts_for_athlete(a, event_date), athletes))

        # Warm the timeseries cache for default picks so Analyze is fast.
        by_id = {a["id"]: a for a in athletes}
        for row in rows:
            wid = row["default_workout_id"]
            if wid:
                threading.Thread(target=_precache, args=(by_id[row["athlete_id"]], wid), daemon=True).start()

        return templates.TemplateResponse(
            "partials/race_difficulty_workouts.html",
            {
                "request": request,
                "race_key": f"{key[0]}:{key[1]}",
                "event": header,
                "rows": rows,
            },
        )

    # ── Step 3: analysis payload (streams + metadata) ─────────────────────────

    def _load_stream(athlete: dict, workout_id: str, weight_kg: float) -> tuple[dict | None, str | None]:
        if workout_id.startswith("manual_"):
            payload = get_cached_timeseries(workout_id)
            if payload is None:
                return None, "Uploaded FIT no longer cached — re-upload it"
        else:
            if not athlete["tp_athlete_id"]:
                return None, "No TrainingPeaks link"
            try:
                api = get_api(athlete["id"])
                payload = fetch_timeseries_cached(api, workout_id, int(athlete["tp_athlete_id"]))
            except Exception as e:  # noqa: BLE001
                return None, f"Timeseries fetch failed: {e}"
        return build_athlete_stream(payload, weight_kg)

    @app.get("/partials/race_difficulty/analysis", response_class=HTMLResponse)
    def rd_analysis(
        request: Request,
        race_key: str | None = Query(default=None),
        athlete_ids: list[int] = Query(default=[]),
        coach_id: int = Depends(require_coach),
    ):
        key = parse_event_prog_key(race_key)
        if not key:
            return HTMLResponse(_error_card("No race selected."))

        athletes = _selected_athletes(coach_id, athlete_ids)
        if not athletes:
            return HTMLResponse(_error_card("No selected athletes found in your roster."))

        qp = request.query_params
        warnings: list[dict] = []
        tasks: list[tuple[dict, str, float]] = []
        for a in athletes:
            if qp.get(f"include_{a['id']}") is None:
                continue
            wid = (qp.get(f"workout_{a['id']}") or "").strip()
            weight = _parse_weight(qp.get(f"weight_{a['id']}"))
            if not wid:
                warnings.append({"name": a["name"], "reason": "No workout selected"})
                continue
            if weight is None:
                warnings.append({"name": a["name"], "reason": "No weight provided"})
                continue
            tasks.append((a, wid, weight))

        if not tasks:
            return HTMLResponse(_error_card("No athletes to analyze — each needs a workout, a weight, and the Include box ticked."))

        with ThreadPoolExecutor(max_workers=min(len(tasks), MAX_FANOUT_WORKERS)) as pool:
            results = list(pool.map(lambda t: _load_stream(*t), tasks))

        athletes_payload = []
        for (a, wid, weight), (stream, reason) in zip(tasks, results):
            if stream is None:
                warnings.append({"name": a["name"], "reason": reason or "Could not load stream"})
                continue
            athletes_payload.append({
                "athlete_id": a["id"],
                "name": a["name"],
                "color": PALETTE[len(athletes_payload)] if len(athletes_payload) < len(PALETTE) else PALETTE_OVERFLOW,
                "weight_kg": weight,
                "workout_id": wid,
                **stream,
            })

        if not athletes_payload:
            return templates.TemplateResponse(
                "partials/race_difficulty_analysis.html",
                {"request": request, "payload": None, "warnings": warnings},
            )

        engine = get_triathlon_engine()
        header = fetch_event_header(key[0], key[1], engine) if engine is not None else None
        race = {
            "event_id": key[0],
            "prog_id": key[1],
            "event_name": (header or {}).get("event_name") or "Race",
            "prog_name": (header or {}).get("prog_name") or "",
            "event_date": str((header or {}).get("event_date") or ""),
        }

        return templates.TemplateResponse(
            "partials/race_difficulty_analysis.html",
            {
                "request": request,
                "payload": {"race": race, "athletes": athletes_payload},
                "warnings": warnings,
            },
        )
