"""Country medal-table routes (`/medals`).

Public, no auth. Wire-up: import and call ``register_medal_routes(app, templates)``
from inside ``app/webapp/app.py::create_app()``.
"""
from __future__ import annotations

from urllib.parse import urlencode

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
    MedalTrend,
    build_country_medalists,
    build_medal_table,
    build_medal_trend,
)


CACHE_HEADERS = {"Cache-Control": "public, max-age=600"}

# Chart chrome (gridlines, ticks, surface) matches the dataviz reference palette's
# light-mode chart chrome — this app has no dark mode, so no dark variant needed.
_TREND_GRID = "#e1e0d9"
_TREND_AXIS_LINE = "#c3c2b7"
_TREND_TICK_TEXT = "#898781"
_TREND_INK = "#0b0b0b"
_TREND_SURFACE = "#fcfcfb"


def _build_trend_figure(trend: MedalTrend | None) -> str | None:
    """Render a MedalTrend into a Plotly figure JSON string (or None if empty)."""
    if not trend or not trend.has_data:
        return None

    import plotly.graph_objects as go

    fig = go.Figure()
    for s in trend.series:
        fig.add_trace(go.Scatter(
            x=trend.years,
            y=s.totals,
            mode="lines+markers",
            name=s.country,
            line=dict(color=s.color, width=2),
            marker=dict(size=8, color=s.color, line=dict(width=2, color=_TREND_SURFACE)),
            hovertemplate="%{y} medal(s)<extra>" + s.country + "</extra>",
        ))
    fig.update_xaxes(
        title_text="Year", dtick=1, gridcolor=_TREND_GRID, linecolor=_TREND_AXIS_LINE,
        tickfont=dict(color=_TREND_TICK_TEXT),
    )
    fig.update_yaxes(
        title_text="Medals", rangemode="tozero", gridcolor=_TREND_GRID,
        linecolor=_TREND_AXIS_LINE, tickfont=dict(color=_TREND_TICK_TEXT),
    )
    fig.update_layout(
        height=380,
        margin=dict(l=50, r=20, t=10, b=40),
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.22),
        plot_bgcolor=_TREND_SURFACE,
        paper_bgcolor=_TREND_SURFACE,
        font=dict(color=_TREND_INK),
    )
    return fig.to_json()

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


def _filter_query_string(gender: str, since: str, cats: set[str], fields: set[str]) -> str:
    """Query string carrying the active filters, for per-row drill-down requests
    that hit a different endpoint than the main filter form.
    """
    params = [("gender", gender), ("since", since)]
    params += [("cats", c) for c in sorted(cats)]
    params += [("fields", f) for f in sorted(fields)]
    return urlencode(params)


def _context(gender: str, since: str, cats: set[str], fields: set[str]) -> dict:
    engine = get_triathlon_engine()
    no_db = engine is None
    table = None
    trend = None
    if engine is not None:
        table = build_medal_table(
            engine,
            gender=gender,
            years_back=SINCE_OPTIONS.get(since),
            categories=cats or None,
            fields=fields or None,
        )
        # Trend intentionally ignores `since` — the whole point is the multi-year view.
        trend = build_medal_trend(
            engine,
            gender=gender,
            categories=cats or None,
            fields=fields or None,
        )
    field_labels = " + ".join(FIELD_LABELS[f] for f in FIELD_KEYS if f in fields)
    return {
        "table": table,
        "trend": trend,
        "trend_fig_json": _build_trend_figure(trend),
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
        "filter_qs": _filter_query_string(gender, since, cats, fields),
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

    @app.get("/partials/medals/country/{country}", response_class=HTMLResponse)
    def medals_country_partial(
        request: Request,
        country: str,
        gender: str | None = None,
        since: str | None = None,
        cats: list[str] = Query(default=[]),
        fields: list[str] = Query(default=[]),
    ):
        g, s, c, f = _parse(gender, since, cats, fields)
        engine = get_triathlon_engine()
        athletes = []
        if engine is not None:
            athletes = build_country_medalists(
                engine, country,
                gender=g,
                years_back=SINCE_OPTIONS.get(s),
                categories=c or None,
                fields=f or None,
            )
        return templates.TemplateResponse(
            "partials/medals_country_athletes.html",
            {"request": request, "country": country, "athletes": athletes},
            headers=CACHE_HEADERS,
        )
