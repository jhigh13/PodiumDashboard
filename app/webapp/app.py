from __future__ import annotations

from datetime import date, timedelta
import hashlib
from typing import Optional

import requests
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select

from app.auth.oauth import fetch_token, get_authorization_url
from app.data.db import init_db, get_session
from app.models.tables import Athlete, CoachRosterMember, DailyMetric, MetricAlert, Workout
from app.services.athletes import upsert_athlete
from app.services.jobs import enqueue_job, get_job
from app.services import compliance as compliance_service
from app.utils.dates import get_effective_today
from app.utils.settings import settings


templates = Jinja2Templates(directory="app/webapp/templates")


COACH_SCOPES = [
    # TrainingPeaks may reject requests that combine coach + athlete account access.
    # Keep coach login limited to coach scopes + data read scopes (mirrors Streamlit).
    "coach:athletes",
    "metrics:read",
    "workouts:read",
    "workouts:details",
    "workouts:wod",
]

ATHLETE_SCOPES = [
    "athlete:profile",
    "metrics:read",
    "workouts:read",
    "workouts:details",
    "workouts:wod",
]


def _parse_date(value: str | None) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _get_session_athlete_id(request: Request) -> Optional[int]:
    raw = request.session.get("athlete_id")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _extract_tp_athlete_id(profile: object) -> Optional[int]:
    if not isinstance(profile, dict):
        return None
    candidates = [
        profile.get("athleteId"),
        profile.get("athlete_id"),
        profile.get("athleteID"),
        profile.get("id"),
    ]
    nested = profile.get("athlete")
    if isinstance(nested, dict):
        candidates.extend([nested.get("athleteId"), nested.get("id")])
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def _get_or_create_local_identity(external_id: str, name: str | None = None, email: str | None = None) -> Athlete:
    with get_session() as session:
        existing = session.execute(select(Athlete).where(Athlete.external_id == external_id)).scalars().first()
        if existing:
            if name and existing.name != name:
                existing.name = name
            if email and existing.email != email:
                existing.email = email
            session.commit()
            session.refresh(existing)
            return existing
        athlete = Athlete(
            external_id=external_id,
            name=name or external_id,
            email=email,
        )
        session.add(athlete)
        session.commit()
        session.refresh(athlete)
        return athlete


def require_login(request: Request) -> int:
    athlete_id = _get_session_athlete_id(request)
    if not athlete_id:
        raise HTTPException(status_code=401, detail="Not logged in")
    return athlete_id


def require_coach(request: Request, athlete_id: int = Depends(require_login)) -> int:
    role = request.session.get("role")
    if role != "coach":
        raise HTTPException(status_code=403, detail="Coach access required")
    return athlete_id


def can_access_athlete(requester_id: int, role: str, target_athlete_id: int) -> bool:
    if role == "athlete":
        return int(requester_id) == int(target_athlete_id)
    if role == "coach":
        with get_session() as session:
            stmt = select(CoachRosterMember).where(
                CoachRosterMember.coach_athlete_id == int(requester_id),
                CoachRosterMember.athlete_id == int(target_athlete_id),
            )
            return session.execute(stmt).scalars().first() is not None
    return False


def create_app() -> FastAPI:
    init_db()

    app = FastAPI(title="Podium Dashboard Web")
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        athlete_id = _get_session_athlete_id(request)
        if not athlete_id:
            return RedirectResponse(url="/login", status_code=302)
        role = request.session.get("role")
        return RedirectResponse(url="/coach" if role == "coach" else "/me", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "tp_auth_base": settings.tp_auth_base,
            },
        )

    @app.post("/login/start")
    def start_login(request: Request, role: str = Form(...)):
        role_norm = (role or "").strip().lower()
        if role_norm not in {"athlete", "coach"}:
            raise HTTPException(status_code=400, detail="Invalid role")

        scopes = COACH_SCOPES if role_norm == "coach" else ATHLETE_SCOPES
        auth_url, state = get_authorization_url(scope=scopes, redirect_uri=settings.tp_web_redirect_uri)

        request.session["oauth_state"] = state
        request.session["oauth_role"] = role_norm
        return RedirectResponse(url=auth_url, status_code=302)

    @app.get("/oauth/callback")
    def oauth_callback(request: Request, code: str | None = None, state: str | None = None):
        if not code:
            raise HTTPException(status_code=400, detail="Missing code")

        expected_state = request.session.get("oauth_state")
        if expected_state and state and expected_state != state:
            raise HTTPException(status_code=400, detail="OAuth state mismatch")

        # NOTE: The browser session cookie is host-bound. If you browse to 127.0.0.1
        # but TP redirects to localhost (or vice-versa), the session won't be available
        # here. So we treat the session role as a hint and also infer from token scope.
        role_hint = request.session.get("oauth_role")

        token = fetch_token(code, scope=None, redirect_uri=settings.tp_web_redirect_uri)

        token_scope = str(token.get("scope") or "")
        role = (role_hint or ("coach" if "coach:athletes" in token_scope else "athlete")).strip().lower()

        # Some providers include identity fields directly in the token response.
        # Prefer that, especially for coach logins where profile endpoints may require athlete-only scopes.
        tp_athlete_id = _extract_tp_athlete_id(token)

        # Fetch profile to get a stable TP identity.
        headers = {"Authorization": f"Bearer {token.get('access_token')}", "Accept": "application/json"}
        profile_url = f"{settings.tp_api_base.rstrip('/')}/v1/athlete/profile"
        prof = {}
        profile_status = None
        profile_error_snippet = None
        try:
            resp = requests.get(profile_url, headers=headers, timeout=20)
            profile_status = resp.status_code
            if resp.status_code == 200:
                prof = resp.json() or {}
            else:
                profile_error_snippet = (resp.text or "").strip()[:300]
        except Exception:
            prof = {}

        if not tp_athlete_id:
            tp_athlete_id = _extract_tp_athlete_id(prof)

        name = prof.get("name") if isinstance(prof, dict) else None
        email = prof.get("email") if isinstance(prof, dict) else None

        if not tp_athlete_id:
            # Coach tokens may not be allowed to access /v1/athlete/profile (or TP may reject mixed scopes).
            # We still allow login by creating a stable local identity keyed off the refresh token.
            if role == "coach":
                raw = token.get("refresh_token") or token.get("access_token") or ""
                digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12] if raw else "unknown"
                athlete = _get_or_create_local_identity(
                    external_id=f"tp_coach_{digest}",
                    name=name or "TrainingPeaks Coach",
                    email=email,
                )
            else:
                detail = "Could not resolve TrainingPeaks athlete id from profile"
                if profile_status and profile_status != 200:
                    detail += f" (profile HTTP {profile_status})"
                if profile_error_snippet:
                    detail += f". Body: {profile_error_snippet}"
                raise HTTPException(status_code=400, detail=detail)
        else:
            athlete = upsert_athlete(tp_athlete_id=int(tp_athlete_id), name=name, email=email)

        # Store token under this athlete identity (reuses existing tokens system).
        from app.services.tokens import store_token

        store_token(athlete.id, token)

        request.session.pop("oauth_state", None)
        request.session.pop("oauth_role", None)
        request.session["athlete_id"] = int(athlete.id)
        request.session["role"] = "coach" if role == "coach" else "athlete"

        # Option A: on first coach login, automatically enqueue roster sync.
        if request.session["role"] == "coach":
            enqueue_job("sync_roster", requested_by_athlete_id=int(athlete.id))
            return RedirectResponse(url="/coach", status_code=302)

        return RedirectResponse(url="/me", status_code=302)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=302)

    @app.get("/me", response_class=HTMLResponse)
    def me_page(request: Request, athlete_id: int = Depends(require_login)):
        role = request.session.get("role")
        if role != "athlete":
            return RedirectResponse(url="/coach", status_code=302)
        today = get_effective_today()
        default_end = request.query_params.get("end") or today.isoformat()
        default_start = request.query_params.get("start") or (today - timedelta(days=14)).isoformat()
        with get_session() as session:
            athlete = session.get(Athlete, int(athlete_id))
        return templates.TemplateResponse(
            "me.html",
            {
                "request": request,
                "athlete": athlete,
                "default_start": default_start,
                "default_end": default_end,
            },
        )

    @app.get("/coach", response_class=HTMLResponse)
    def coach_page(request: Request, coach_id: int = Depends(require_coach)):
        today = get_effective_today()
        default_end = request.query_params.get("end") or today.isoformat()
        default_start = request.query_params.get("start") or (today - timedelta(days=14)).isoformat()
        with get_session() as session:
            coach = session.get(Athlete, int(coach_id))
            stmt = (
                select(Athlete)
                .join(CoachRosterMember, CoachRosterMember.athlete_id == Athlete.id)
                .where(CoachRosterMember.coach_athlete_id == int(coach_id))
                .order_by(Athlete.name)
            )
            roster = session.execute(stmt).scalars().all()
        return templates.TemplateResponse(
            "coach.html",
            {
                "request": request,
                "coach": coach,
                "roster": roster,
                "default_start": default_start,
                "default_end": default_end,
            },
        )

    # ---- Partials (HTMX) ----

    @app.get("/partials/job_status", response_class=HTMLResponse)
    def partial_job_status(request: Request, job_id: int, _: int = Depends(require_login)):
        job = get_job(int(job_id))
        return templates.TemplateResponse(
            "partials/job_status.html",
            {"request": request, "job": job},
        )

    @app.get("/partials/workouts", response_class=HTMLResponse)
    def partial_workouts(
        request: Request,
        target_athlete_id: int,
        start: str,
        end: str,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        start_d = _parse_date(start)
        end_d = _parse_date(end)
        if not start_d or not end_d:
            raise HTTPException(status_code=400, detail="Invalid date range")

        with get_session() as session:
            stmt = (
                select(Workout)
                .where(Workout.athlete_id == int(target_athlete_id))
                .where(Workout.date >= start_d)
                .where(Workout.date <= end_d)
                .order_by(Workout.date.desc())
            )
            workouts = session.execute(stmt).scalars().all()

        return templates.TemplateResponse(
            "partials/workouts.html",
            {"request": request, "workouts": workouts},
        )

    @app.get("/partials/metrics", response_class=HTMLResponse)
    def partial_metrics(
        request: Request,
        target_athlete_id: int,
        start: str,
        end: str,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        start_d = _parse_date(start)
        end_d = _parse_date(end)
        if not start_d or not end_d:
            raise HTTPException(status_code=400, detail="Invalid date range")

        with get_session() as session:
            stmt = (
                select(DailyMetric)
                .where(DailyMetric.athlete_id == int(target_athlete_id))
                .where(DailyMetric.date >= start_d)
                .where(DailyMetric.date <= end_d)
                .order_by(DailyMetric.date.desc())
            )
            metrics = session.execute(stmt).scalars().all()

            available = None
            if not metrics:
                from sqlalchemy import func

                min_d = session.execute(
                    select(func.min(DailyMetric.date)).where(DailyMetric.athlete_id == int(target_athlete_id))
                ).scalar_one()
                max_d = session.execute(
                    select(func.max(DailyMetric.date)).where(DailyMetric.athlete_id == int(target_athlete_id))
                ).scalar_one()
                if min_d or max_d:
                    available = {"min": min_d, "max": max_d}

        return templates.TemplateResponse(
            "partials/metrics.html",
            {"request": request, "metrics": metrics, "available": available},
        )

    @app.get("/partials/alerts", response_class=HTMLResponse)
    def partial_alerts(
        request: Request,
        target_athlete_id: int,
        days: int = 7,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        with get_session() as session:
            stmt = (
                select(MetricAlert)
                .where(MetricAlert.athlete_id == int(target_athlete_id))
                .order_by(MetricAlert.alert_date.desc())
                .limit(int(days) * 5)
            )
            alerts = session.execute(stmt).scalars().all()

        return templates.TemplateResponse(
            "partials/alerts.html",
            {"request": request, "alerts": alerts},
        )

    @app.get("/partials/compliance_today", response_class=HTMLResponse)
    def partial_compliance_today(
        request: Request,
        target_athlete_id: int,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        today = get_effective_today()
        snapshot = compliance_service.get_compliance_for_day(int(target_athlete_id), today) or {}
        records = (snapshot.get("records") or [])

        def _classify(record: dict) -> dict:
            actual = record.get("actual") or {}
            completed = actual.get("completed") is True
            score = record.get("overall_score")

            if not completed:
                return {"bucket": "missed", "badge": "Missed", "badge_class": "gray"}
            if not isinstance(score, (int, float)):
                return {"bucket": "unknown", "badge": "No score", "badge_class": "gray"}
            if score >= 85:
                return {"bucket": "good", "badge": "Good", "badge_class": "green"}
            if score >= 70:
                return {"bucket": "ok", "badge": "Ok", "badge_class": "yellow"}
            return {"bucket": "bad", "badge": "Bad", "badge_class": "red"}

        buckets = {"good": 0, "ok": 0, "bad": 0, "missed": 0, "unknown": 0}
        enriched: list[dict] = []
        for r in records:
            c = _classify(r)
            buckets[c["bucket"]] += 1
            enriched.append({**r, "_class": c})

        # Group by sport for readability (Swim/Bike/Run first).
        sport_order = {"swim": 0, "bike": 1, "run": 2}
        enriched.sort(key=lambda r: (sport_order.get(str(r.get("sport") or "").lower(), 99), str(r.get("sport") or "")))

        return templates.TemplateResponse(
            "partials/compliance_today.html",
            {
                "request": request,
                "today": today,
                "records": enriched,
                "buckets": buckets,
                "total": len(enriched),
            },
        )

    # ---- Jobs ----

    @app.post("/jobs/sync_recent")
    def sync_recent_job(
        request: Request,
        target_athlete_id: int = Form(...),
        days: int = Form(7),
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, int(target_athlete_id)):
            raise HTTPException(status_code=403, detail="Forbidden")

        job = enqueue_job(
            "sync_recent",
            requested_by_athlete_id=int(requester_id),
            target_athlete_id=int(target_athlete_id),
            payload={"days": int(days)},
        )

        return templates.TemplateResponse(
            "partials/job_enqueued.html",
            {"request": request, "job_id": int(job.id)},
        )

    return app


app = create_app()
