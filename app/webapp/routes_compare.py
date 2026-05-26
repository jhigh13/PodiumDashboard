"""Athlete head-to-head comparison routes (`/compare`).

Public, no auth. Wire-up: import and call ``register_compare_routes(app, templates)``
from inside ``app/webapp/app.py::create_app()``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.data.triathlon_db import get_triathlon_engine
from app.services.athlete_index import get_athlete_index
from app.services.head_to_head import (
    AthleteRef,
    CompareBundle,
    build_compare_bundle,
    resolve_athlete,
)
from app.services.race_detail import RaceAthleteData, get_race_detail
from app.utils.timefmt import (
    distance_label,
    format_gap,
    format_segment_time,
    humanize_elapsed_since,
    seconds_to_hms,
)


CACHE_HEADERS = {"Cache-Control": "public, max-age=600"}


def _resolve_pair(a: str | int | None, b: str | int | None) -> tuple[Optional[AthleteRef], Optional[AthleteRef]]:
    return resolve_athlete(a), resolve_athlete(b)


# Date-range filter mapping: URL param -> years_back integer
_SINCE_OPTIONS: dict[str, int | None] = {
    "": None, "all": None,
    "1y": 1, "2y": 2, "3y": 3, "4y": 4, "5y": 5,
}


def _parse_since(since: str | None) -> int | None:
    if since is None:
        return None
    return _SINCE_OPTIONS.get(since.strip().lower(), None)


def _get_bundle_or_none(a: int, b: int, since: str | None = None) -> CompareBundle | None:
    engine = get_triathlon_engine()
    if engine is None:
        return None
    try:
        return build_compare_bundle(int(a), int(b), engine=engine, years_back=_parse_since(since))
    except ValueError:
        return None


def _from_today_days(d) -> int | None:
    from datetime import date as _date
    if d is None:
        return None
    if not isinstance(d, _date):
        return None
    return (_date.today() - d).days


def register_compare_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    # Expose helpers in Jinja for templates.
    templates.env.globals["format_segment_time"] = format_segment_time
    templates.env.globals["format_gap"] = format_gap
    templates.env.globals["distance_label"] = distance_label
    templates.env.globals["humanize_elapsed_since"] = humanize_elapsed_since
    templates.env.globals["from_today_days"] = _from_today_days

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(
        request: Request,
        a: str | None = None,
        b: str | None = None,
        a_slug: str | None = None,
        b_slug: str | None = None,
        since: str | None = None,
    ):
        # Picker submits BOTH the typed text (a, b) and the hidden slug
        # (a_slug, b_slug). Prefer the slug — it's what the user actually
        # picked from the dropdown. Fall back to the typed text, which
        # resolve_athlete slugifies if needed.
        a_value = (a_slug or a or "").strip() or None
        b_value = (b_slug or b or "").strip() or None
        athlete_a, athlete_b = _resolve_pair(a_value, b_value)
        no_db = get_triathlon_engine() is None
        # Normalize since to a known value or empty
        since_norm = (since or "").strip().lower()
        if since_norm not in _SINCE_OPTIONS:
            since_norm = ""
        return templates.TemplateResponse(
            "compare.html",
            {
                "request": request,
                "title": "Athlete Compare",
                "athlete_a": athlete_a,
                "athlete_b": athlete_b,
                "a_param": a_value or "",
                "b_param": b_value or "",
                "since": since_norm,
                "no_db": no_db,
            },
        )

    @app.get("/partials/compare/search", response_class=HTMLResponse)
    def compare_search(
        request: Request,
        q: str = "",
        a: str = "",
        b: str = "",
        slot: str = "a",
    ):
        # The picker inputs have name="a" / name="b", so HTMX sends the value
        # under those param names rather than as "q". Accept any of them.
        query = (q or a or b or "").strip()
        idx = get_athlete_index()
        entries = idx.search(query, limit=10) if query else []
        return templates.TemplateResponse(
            "partials/compare_search_results.html",
            {"request": request, "entries": entries, "slot": slot, "q": query},
        )

    @app.get("/partials/compare/h2h", response_class=HTMLResponse)
    def partial_h2h(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load comparison."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_h2h.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/how_they_win", response_class=HTMLResponse)
    def partial_how_they_win(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load segment wins."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_how_they_win.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/avg_splits", response_class=HTMLResponse)
    def partial_avg_splits(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load splits."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_avg_splits.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/pack_profile", response_class=HTMLResponse)
    def partial_pack_profile(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load pack profile."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_pack_profile.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/transitions", response_class=HTMLResponse)
    def partial_transitions(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load transitions."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_transitions.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/race_log", response_class=HTMLResponse)
    def partial_race_log(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load race log."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_race_log.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/context", response_class=HTMLResponse)
    def partial_context(request: Request, a: int, b: int, since: str | None = None):
        bundle = _get_bundle_or_none(a, b, since=since)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load context."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_context.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/compare/race/{event_id}/{prog_id}", response_class=HTMLResponse)
    def race_detail_page(
        request: Request,
        event_id: int,
        prog_id: int,
        a: str | None = None,
        b: str | None = None,
    ):
        athlete_a = resolve_athlete(a)
        athlete_b = resolve_athlete(b)
        engine = get_triathlon_engine()
        if engine is None or athlete_a is None or athlete_b is None:
            return templates.TemplateResponse(
                "compare_race_detail.html",
                {
                    "request": request,
                    "title": "Race detail",
                    "athlete_a": athlete_a,
                    "athlete_b": athlete_b,
                    "detail": None,
                    "error": "Could not resolve athletes or database not configured.",
                },
            )
        detail = get_race_detail(
            event_id, prog_id,
            athlete_a.athlete_id, athlete_b.athlete_id,
            engine=engine,
        )
        pos_traces, pos_layout, gap_traces, gap_layout = _build_race_charts(detail, athlete_a, athlete_b)
        return templates.TemplateResponse(
            "compare_race_detail.html",
            {
                "request": request,
                "title": (detail.event.event_name if detail else "Race detail"),
                "athlete_a": athlete_a,
                "athlete_b": athlete_b,
                "detail": detail,
                "pos_traces": pos_traces,
                "pos_layout": pos_layout,
                "gap_traces": gap_traces,
                "gap_layout": gap_layout,
                "error": None,
            },
        )


def _error_card(msg: str) -> str:
    return f'<div class="card"><p class="muted">{msg}</p></div>'


_STAGE_LABELS = ["Swim", "T1", "Bike", "T2", "Run"]


def _build_race_charts(detail, athlete_a, athlete_b):
    """Construct Plotly trace/layout dicts for the position + gap charts."""
    if detail is None:
        return [], {}, [], {}

    def _series(athlete: RaceAthleteData | None, attr_prefix: str):
        if athlete is None:
            return [None] * 5
        return [
            getattr(athlete, f"{attr_prefix}_swim"),
            getattr(athlete, f"{attr_prefix}_t1"),
            getattr(athlete, f"{attr_prefix}_bike"),
            getattr(athlete, f"{attr_prefix}_t2"),
            getattr(athlete, f"{attr_prefix}_run"),
        ]

    a_pos = _series(detail.athlete_a, "pos")
    b_pos = _series(detail.athlete_b, "pos")
    a_gap = _series(detail.athlete_a, "gap")
    b_gap = _series(detail.athlete_b, "gap")

    a_name = athlete_a.full_name if athlete_a else "Athlete A"
    b_name = athlete_b.full_name if athlete_b else "Athlete B"
    a_color = "#4f46e5"
    b_color = "#15803d"
    a_dash = "solid" if detail.athlete_a and detail.athlete_a.finish_status in {"FINISH", "FINISHED", "OK", "OFFICIAL"} else "dot"
    b_dash = "solid" if detail.athlete_b and detail.athlete_b.finish_status in {"FINISH", "FINISHED", "OK", "OFFICIAL"} else "dot"

    common_axis = {"showgrid": True, "gridcolor": "#e2e8f0"}

    pos_traces = [
        {
            "x": _STAGE_LABELS, "y": a_pos, "name": a_name,
            "mode": "lines+markers", "type": "scatter",
            "line": {"color": a_color, "width": 3, "dash": a_dash},
            "marker": {"size": 11, "color": a_color},
            "connectgaps": False,
        },
        {
            "x": _STAGE_LABELS, "y": b_pos, "name": b_name,
            "mode": "lines+markers", "type": "scatter",
            "line": {"color": b_color, "width": 3, "dash": b_dash},
            "marker": {"size": 11, "color": b_color},
            "connectgaps": False,
        },
    ]
    pos_layout = {
        "title": "Position by Stage",
        "xaxis": {"title": "", **common_axis},
        "yaxis": {"title": "Position (1 = leader)", "autorange": "reversed", **common_axis},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": -0.15},
        "margin": {"l": 60, "r": 20, "t": 50, "b": 60},
        "paper_bgcolor": "white", "plot_bgcolor": "white",
    }

    gap_traces = [
        {
            "x": _STAGE_LABELS, "y": a_gap, "name": a_name,
            "mode": "lines+markers", "type": "scatter",
            "line": {"color": a_color, "width": 3, "dash": a_dash},
            "marker": {"size": 11, "color": a_color},
            "connectgaps": False,
        },
        {
            "x": _STAGE_LABELS, "y": b_gap, "name": b_name,
            "mode": "lines+markers", "type": "scatter",
            "line": {"color": b_color, "width": 3, "dash": b_dash},
            "marker": {"size": 11, "color": b_color},
            "connectgaps": False,
        },
    ]
    gap_layout = {
        "title": "Gap to Leader by Stage",
        "xaxis": {"title": "", **common_axis},
        "yaxis": {"title": "Gap (seconds)", "rangemode": "tozero", **common_axis},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": -0.15},
        "margin": {"l": 60, "r": 20, "t": 50, "b": 60},
        "paper_bgcolor": "white", "plot_bgcolor": "white",
    }
    return pos_traces, pos_layout, gap_traces, gap_layout
