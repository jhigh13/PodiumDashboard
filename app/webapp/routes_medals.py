"""Country medal-table routes (`/medals`).

Public, no auth. Wire-up: import and call ``register_medal_routes(app, templates)``
from inside ``app/webapp/app.py::create_app()``.
"""
from __future__ import annotations

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.data.triathlon_db import get_triathlon_engine
from app.services.medal_table import (
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    DEFAULT_FIELDS,
    FIELD_KEYS,
    FIELD_LABELS,
    GENDER_KEYS,
    SINCE_OPTIONS,
    build_medal_table,
)


CACHE_HEADERS = {"Cache-Control": "public, max-age=600"}

_SINCE_LABELS: dict[str, str] = {
    "": "All time",
    "8y": "Last 8 years",
    "5y": "Last 5 years",
    "4y": "Last 4 years",
    "3y": "Last 3 years",
    "2y": "Last 2 years",
    "1y": "Last 1 year",
}


def _normalize_keys(values: list[str] | None, valid: tuple[str, ...]) -> set[str]:
    if not values:
        return set()
    out: set[str] = set()
    for raw in values:
        if raw is None:
            continue
        for piece in str(raw).split(","):
            v = piece.strip().lower()
            if v in valid:
                out.add(v)
    return out


def _context(gender: str, since: str, cats: set[str], fields: set[str]) -> dict:
    engine = get_triathlon_engine()
    no_db = engine is None
    table = None
    if engine is not None:
        table = build_medal_table(
            engine,
            gender=gender,
            years_back=SINCE_OPTIONS.get(since),
            categories=cats or None,
            fields=fields or None,
        )
    field_labels = " + ".join(FIELD_LABELS[f] for f in FIELD_KEYS if f in fields)
    return {
        "table": table,
        "no_db": no_db,
        "gender": gender,
        "since": since,
        "since_label": _SINCE_LABELS.get(since, "All time"),
        "cats": cats,
        "fields": fields,
        "field_label": field_labels or FIELD_LABELS["elite"],
        "gender_options": GENDER_KEYS,
        "category_options": [(k, CATEGORY_LABELS[k]) for k in CATEGORY_KEYS],
        "category_labels": CATEGORY_LABELS,
        "field_options": [(k, FIELD_LABELS[k]) for k in FIELD_KEYS],
        "since_options": list(_SINCE_LABELS.items()),
    }


def register_medal_routes(app: FastAPI, templates: Jinja2Templates) -> None:

    def _parse(gender: str | None, since: str | None, cats, fields):
        g = (gender or "all").strip().lower()
        if g not in GENDER_KEYS:
            g = "all"
        s = (since or "").strip().lower()
        if s not in SINCE_OPTIONS:
            s = ""
        f = _normalize_keys(fields, FIELD_KEYS) or set(DEFAULT_FIELDS)
        return g, s, _normalize_keys(cats, CATEGORY_KEYS), f

    @app.get("/medals", response_class=HTMLResponse)
    def medals_page(
        request: Request,
        gender: str | None = None,
        since: str | None = None,
        cats: list[str] = Query(default=[]),
        fields: list[str] = Query(default=[]),
    ):
        g, s, c, f = _parse(gender, since, cats, fields)
        ctx = _context(g, s, c, f)
        ctx.update({"request": request, "title": "Medal Table"})
        return templates.TemplateResponse("medals.html", ctx)

    @app.get("/partials/medals", response_class=HTMLResponse)
    def medals_partial(
        request: Request,
        gender: str | None = None,
        since: str | None = None,
        cats: list[str] = Query(default=[]),
        fields: list[str] = Query(default=[]),
    ):
        g, s, c, f = _parse(gender, since, cats, fields)
        ctx = _context(g, s, c, f)
        ctx["request"] = request
        return templates.TemplateResponse(
            "partials/medals_results.html", ctx, headers=CACHE_HEADERS,
        )
