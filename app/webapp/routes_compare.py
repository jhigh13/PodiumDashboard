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
from app.utils.timefmt import (
    distance_label,
    format_gap,
    format_segment_time,
    humanize_elapsed_since,
)


CACHE_HEADERS = {"Cache-Control": "public, max-age=600"}


def _resolve_pair(a: str | int | None, b: str | int | None) -> tuple[Optional[AthleteRef], Optional[AthleteRef]]:
    return resolve_athlete(a), resolve_athlete(b)


def _get_bundle_or_none(a: int, b: int) -> CompareBundle | None:
    engine = get_triathlon_engine()
    if engine is None:
        return None
    try:
        return build_compare_bundle(int(a), int(b), engine=engine)
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
    def compare_page(request: Request, a: str | None = None, b: str | None = None):
        athlete_a, athlete_b = _resolve_pair(a, b)
        no_db = get_triathlon_engine() is None
        return templates.TemplateResponse(
            "compare.html",
            {
                "request": request,
                "title": "Athlete Compare",
                "athlete_a": athlete_a,
                "athlete_b": athlete_b,
                "a_param": a or "",
                "b_param": b or "",
                "no_db": no_db,
            },
        )

    @app.get("/partials/compare/search", response_class=HTMLResponse)
    def compare_search(request: Request, q: str = "", slot: str = "a"):
        idx = get_athlete_index()
        entries = idx.search(q, limit=10) if q.strip() else []
        return templates.TemplateResponse(
            "partials/compare_search_results.html",
            {"request": request, "entries": entries, "slot": slot, "q": q},
        )

    @app.get("/partials/compare/h2h", response_class=HTMLResponse)
    def partial_h2h(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load comparison."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_h2h.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/how_they_win", response_class=HTMLResponse)
    def partial_how_they_win(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load segment wins."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_how_they_win.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/avg_splits", response_class=HTMLResponse)
    def partial_avg_splits(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load splits."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_avg_splits.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/pack_profile", response_class=HTMLResponse)
    def partial_pack_profile(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load pack profile."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_pack_profile.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/transitions", response_class=HTMLResponse)
    def partial_transitions(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load transitions."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_transitions.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )

    @app.get("/partials/compare/race_log", response_class=HTMLResponse)
    def partial_race_log(request: Request, a: int, b: int):
        bundle = _get_bundle_or_none(a, b)
        if bundle is None:
            return HTMLResponse(_error_card("Could not load race log."), headers=CACHE_HEADERS)
        return templates.TemplateResponse(
            "partials/compare_race_log.html",
            {"request": request, "bundle": bundle},
            headers=CACHE_HEADERS,
        )


def _error_card(msg: str) -> str:
    return f'<div class="card"><p class="muted">{msg}</p></div>'
