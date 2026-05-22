from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta, datetime
import math
import hashlib
import json
from typing import Optional

import requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, delete, text

from app.auth.oauth import fetch_token, get_authorization_url
from app.data.db import init_db, get_session
from app.models.tables import (
    Athlete,
    CoachRosterMember,
    DailyMetric,
    MetricAlert,
    RecoveryAlertRun,
    Workout,
    WorkoutCompliance,
    WorkoutDetail,
    WorkoutLap,
    WTODashboardAthleteMap,
    WTORaceResult,
)
from app.services.athletes import upsert_athlete
from app.services.jobs import enqueue_job, get_job
from app.services import compliance as compliance_service
from app.services.baseline import calculate_baselines, check_alert_conditions, get_baseline_asof
from app.services.tp_api import get_api
from app.services.tokens import get_token as get_token_row, find_coach_token
from app.services.race_results import (
    load_local_race_results,
    pick_best_worst,
    sync_race_results_last_two_years,
)
from app.services.workout_cache import (
    fetch_timeseries_cached,
    normalize_timeseries_rows,
    extract_workout_summary,
    extract_lap_summaries,
    is_timeseries_cached,
    parse_fit_to_timeseries,
    save_timeseries,
)
from app.services.recovery_alerts import evaluate_recovery_alert
from app.services.recovery_alert_runs import list_recovery_alert_runs, upsert_recovery_alert_run
from app.data.triathlon_db import get_triathlon_engine
from app.utils.dates import get_effective_today
from app.utils.settings import settings
from app.scheduling.scheduler import start_scheduler, stop_scheduler
from app.webapp.routes_compare import register_compare_routes


templates = Jinja2Templates(directory="app/webapp/templates")
# Disable Jinja2 LRU cache to avoid "unhashable type: dict" errors when
# Starlette passes request context through the template cache key path.
templates.env.auto_reload = True
templates.env.cache = None


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


def _coerce_date(value: object) -> Optional[date]:
    """Best-effort convert API date fields to a date.

    TrainingPeaks responses sometimes include timestamps (YYYY-MM-DDTHH:MM:SS).
    """
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value)
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    try:
        return date.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None


def _is_truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in {"1", "true", "t", "yes", "y", "on"}


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
        profile.get("userId"),
        profile.get("user_id"),
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


def _merge_coach_roster(from_coach_id: int, to_coach_id: int) -> None:
    """Move roster memberships from one coach identity to another.

    This prevents a common issue where a coach login falls back to a local identity
    (external_id like tp_coach_*) and later logins resolve to a different local identity.
    """
    if int(from_coach_id) == int(to_coach_id):
        return
    with get_session() as session:
        # Avoid unique constraint violations by removing overlaps first.
        from_members = session.execute(
            select(CoachRosterMember.athlete_id).where(CoachRosterMember.coach_athlete_id == int(from_coach_id))
        ).scalars().all()
        if not from_members:
            return
        existing_members = set(
            session.execute(
                select(CoachRosterMember.athlete_id).where(CoachRosterMember.coach_athlete_id == int(to_coach_id))
            ).scalars().all()
        )
        # Insert missing
        for athlete_id in from_members:
            if int(athlete_id) in existing_members:
                continue
            session.add(CoachRosterMember(coach_athlete_id=int(to_coach_id), athlete_id=int(athlete_id)))
        # Delete old memberships
        session.execute(delete(CoachRosterMember).where(CoachRosterMember.coach_athlete_id == int(from_coach_id)))
        session.commit()


def _update_athlete_fields(athlete_id: int, *, name: str | None = None, email: str | None = None, tp_athlete_id: int | None = None) -> None:
    with get_session() as session:
        athlete = session.get(Athlete, int(athlete_id))
        if not athlete:
            return
        changed = False
        if name and athlete.name != name:
            athlete.name = name
            changed = True
        if email and athlete.email != email:
            athlete.email = email
            changed = True
        if tp_athlete_id and getattr(athlete, "tp_athlete_id", None) != int(tp_athlete_id):
            athlete.tp_athlete_id = int(tp_athlete_id)
            changed = True
        if changed:
            session.commit()


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

    @app.on_event("startup")
    def _startup_scheduler() -> None:
        if settings.enable_scheduler:
            start_scheduler()

    @app.on_event("shutdown")
    def _shutdown_scheduler() -> None:
        if settings.enable_scheduler:
            stop_scheduler()

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        athlete_id = _get_session_athlete_id(request)
        if not athlete_id:
            return RedirectResponse(url="/rankings", status_code=302)
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

        # For coaches, TP provides a stable CoachId via /v1/coach/profile.
        # Prefer this over token-derived fallbacks so we don't create duplicate local coach identities.
        headers = {"Authorization": f"Bearer {token.get('access_token')}", "Accept": "application/json"}
        coach_tp_id: int | None = None
        if role == "coach":
            coach_profile_url = f"{settings.tp_api_base.rstrip('/')}/v1/coach/profile"
            try:
                coach_resp = requests.get(coach_profile_url, headers=headers, timeout=20)
                if coach_resp.status_code == 200:
                    coach_prof = coach_resp.json() or {}
                    coach_id_raw = None
                    if isinstance(coach_prof, dict):
                        coach_id_raw = coach_prof.get("CoachId") or coach_prof.get("coachId") or coach_prof.get("id")
                    if coach_id_raw is not None:
                        coach_tp_id = int(coach_id_raw)
                        coach_first = coach_prof.get("FirstName") if isinstance(coach_prof, dict) else None
                        coach_last = coach_prof.get("LastName") if isinstance(coach_prof, dict) else None
                        coach_name = " ".join([p for p in [coach_first, coach_last] if p]) or None
                        # Create/reuse a stable local coach identity based on CoachId.
                        athlete = _get_or_create_local_identity(
                            external_id=f"tp_coach_{coach_tp_id}",
                            name=coach_name or "TrainingPeaks Coach",
                        )
            except Exception:
                coach_tp_id = None

        # If we already established a coach identity, we can skip athlete identity resolution.
        tp_athlete_id = None if coach_tp_id is not None else _extract_tp_athlete_id(token)

        # Fetch athlete profile (may fail for coach tokens depending on TP scope rules).
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

        if tp_athlete_id is None:
            tp_athlete_id = _extract_tp_athlete_id(prof)

        name = prof.get("name") if isinstance(prof, dict) else None
        email = prof.get("email") if isinstance(prof, dict) else None

        if coach_tp_id is not None:
            # We already created a stable coach identity from CoachId above.
            # If the athlete profile is accessible, attach its tp_athlete_id/email/name to this coach row.
            if tp_athlete_id:
                _update_athlete_fields(int(athlete.id), name=name, email=email, tp_athlete_id=int(tp_athlete_id))
            else:
                _update_athlete_fields(int(athlete.id), name=name, email=email)
        elif not tp_athlete_id:
            # Coach tokens may not be allowed to access /v1/athlete/profile (or TP may reject mixed scopes).
            # We still allow login by creating a stable local identity keyed off the refresh token.
            if role == "coach":
                # Prefer a stable identity when possible.
                # - If we have an email, key off that (stable across logins).
                # - Else fall back to token-derived digest (may change across reauth).
                if email:
                    external_id = f"tp_coach_email_{email.strip().lower()}"
                else:
                    raw = token.get("refresh_token") or token.get("access_token") or ""
                    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12] if raw else "unknown"
                    external_id = f"tp_coach_{digest}"
                athlete = _get_or_create_local_identity(
                    external_id=external_id,
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

        # If this is a coach login, attempt to merge any prior fallback coach identities
        # (tp_coach_*) that already have a roster into this coach identity.
        if role == "coach":
            with get_session() as session:
                current_roster_count = session.execute(
                    select(CoachRosterMember.id).where(CoachRosterMember.coach_athlete_id == int(athlete.id))
                ).scalars().all()
                has_roster = bool(current_roster_count)

                # Prefer matching by email when possible.
                candidates_stmt = (
                    select(Athlete.id)
                    .where(Athlete.id != int(athlete.id))
                    .where(Athlete.external_id.like("tp_coach%"))
                )
                if athlete.email:
                    candidates_stmt = candidates_stmt.where(Athlete.email == athlete.email)
                candidate_ids = session.execute(candidates_stmt).scalars().all()

                # Only consider candidates that actually have roster rows.
                roster_candidate_ids = []
                for cid in candidate_ids:
                    count = session.execute(
                        select(CoachRosterMember.id).where(CoachRosterMember.coach_athlete_id == int(cid))
                    ).scalars().first()
                    if count is not None:
                        roster_candidate_ids.append(int(cid))

            # If we don't already have a roster, or if there's a clear previous identity,
            # merge roster memberships forward.
            if roster_candidate_ids and (not has_roster or len(roster_candidate_ids) == 1):
                # If multiple, pick the one with the most roster entries.
                best_id = roster_candidate_ids[0]
                if len(roster_candidate_ids) > 1:
                    with get_session() as session:
                        best_id = max(
                            roster_candidate_ids,
                            key=lambda cid: session.execute(
                                select(CoachRosterMember.id).where(CoachRosterMember.coach_athlete_id == int(cid))
                            ).scalars().all().__len__(),
                        )
                _merge_coach_roster(from_coach_id=best_id, to_coach_id=int(athlete.id))

        # Store token under this athlete identity (reuses existing tokens system).
        from app.services.tokens import store_token

        store_token(athlete.id, token)

        request.session.pop("oauth_state", None)
        request.session.pop("oauth_role", None)
        request.session["athlete_id"] = int(athlete.id)
        request.session["role"] = "coach" if role == "coach" else "athlete"

        # Option A: automatically enqueue roster sync if roster is empty.
        if request.session["role"] == "coach":
            with get_session() as session:
                has_roster = (
                    session.execute(
                        select(CoachRosterMember.id).where(CoachRosterMember.coach_athlete_id == int(athlete.id))
                    ).scalars().first()
                    is not None
                )
            if not has_roster:
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
        resp = templates.TemplateResponse(
            "partials/job_status.html",
            {"request": request, "job": job},
        )
        # When a sync finishes, prompt the UI to refresh the currently visible panels.
        # (Elements opt-in via hx-trigger="podiumRefresh from:body".)
        if job and getattr(job, "status", None) == "succeeded":
            resp.headers["HX-Trigger"] = "podiumRefresh"
        return resp

    @app.get("/partials/tp_debug", response_class=HTMLResponse)
    def partial_tp_debug(
        request: Request,
        target_athlete_id: int,
        start: str,
        end: str,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, int(target_athlete_id)):
            raise HTTPException(status_code=403, detail="Forbidden")

        start_d = _parse_date(start)
        end_d = _parse_date(end)
        if not start_d or not end_d:
            raise HTTPException(status_code=400, detail="Invalid date range")

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))
        if not athlete:
            raise HTTPException(status_code=404, detail="Athlete not found")

        tp_athlete_id = getattr(athlete, "tp_athlete_id", None)
        if not tp_athlete_id:
            return templates.TemplateResponse(
                "partials/tp_debug.html",
                {
                    "request": request,
                    "debug": {
                        "athlete_id": int(target_athlete_id),
                        "athlete_name": getattr(athlete, "name", None),
                        "tp_athlete_id": None,
                        "range": f"{start_d.isoformat()}..{end_d.isoformat()}",
                        "has_athlete_token": bool(get_token_row(int(target_athlete_id))),
                        "has_coach_token": bool(find_coach_token()),
                        "note": "This athlete has no tp_athlete_id set, so athlete-scoped v2 endpoints cannot be called.",
                    },
                },
            )

        api = get_api(int(target_athlete_id))
        # Use the same token selection + refresh behavior as normal API calls.
        headers = api._headers()  # noqa: SLF001
        safe_headers = {
            k: ("Bearer ***" if k.lower() == "authorization" else v)
            for k, v in (headers or {}).items()
        }

        base = settings.tp_api_base.rstrip("/")
        workouts_url = f"{base}/v2/workouts/{int(tp_athlete_id)}/{start_d.isoformat()}/{end_d.isoformat()}"
        metrics_url = f"{base}/v2/metrics/{int(tp_athlete_id)}/{start_d.isoformat()}/{end_d.isoformat()}"
        metrics_self_url = f"{base}/v2/metrics/{start_d.isoformat()}/{end_d.isoformat()}"

        def _call(url: str) -> dict:
            out: dict[str, object] = {"url": url}
            try:
                r = requests.get(url, headers=headers, timeout=30)
                out["status"] = int(r.status_code)
                if r.status_code == 200:
                    try:
                        payload = r.json()
                        if isinstance(payload, list):
                            out["count"] = len(payload)
                        else:
                            out["count"] = None
                    except Exception as e:  # noqa: BLE001
                        out["error"] = f"json_decode_error: {e}"
                else:
                    out["body_snippet"] = (r.text or "").strip()[:600]
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)
            return out

        debug = {
            "athlete_id": int(target_athlete_id),
            "athlete_name": getattr(athlete, "name", None),
            "tp_athlete_id": int(tp_athlete_id),
            "range": f"{start_d.isoformat()}..{end_d.isoformat()}",
            "has_athlete_token": bool(get_token_row(int(target_athlete_id))),
            "has_coach_token": bool(find_coach_token()),
            "using_coach_token": bool(getattr(api, "_using_coach_token", False)),
            "headers": safe_headers,
            "workouts": _call(workouts_url),
            "metrics": _call(metrics_url),
            "metrics_self": _call(metrics_self_url),
            "note": "This runs the same athlete-scoped v2 URLs the worker uses. Tokens are masked.",
        }

        return templates.TemplateResponse(
            "partials/tp_debug.html",
            {"request": request, "debug": debug},
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

    @app.get("/partials/recovery_trends", response_class=HTMLResponse)
    def partial_recovery_trends(
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

        # Pull a full year so rolling windows are well-defined.
        calc_start = end_d - timedelta(days=365)

        with get_session() as session:
            stmt = (
                select(DailyMetric)
                .where(DailyMetric.athlete_id == int(target_athlete_id))
                .where(DailyMetric.date >= calc_start)
                .where(DailyMetric.date <= end_d)
                .order_by(DailyMetric.date)
            )
            rows = session.execute(stmt).scalars().all()

        if not rows or len(rows) < 7:
            return templates.TemplateResponse(
                "partials/recovery_trends.html",
                {
                    "request": request,
                    "has_data": False,
                    "message": "Need at least 7 days of daily metrics to show trend charts. Sync metrics first.",
                },
            )

        import pandas as pd
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        df = pd.DataFrame(
            [
                {
                    "date": r.date,
                    "rhr": r.rhr,
                    "hrv": r.hrv,
                    "sleep": r.sleep_hours,
                }
                for r in rows
            ]
        )
        if df.empty:
            return templates.TemplateResponse(
                "partials/recovery_trends.html",
                {"request": request, "has_data": False, "message": "No metrics available for charting."},
            )

        df.sort_values("date", inplace=True)

        # Rolling averages
        df["hrv_7d"] = df["hrv"].rolling(window=7, min_periods=1).mean()
        df["hrv_90d"] = df["hrv"].rolling(window=90, min_periods=1).mean()
        df["rhr_7d"] = df["rhr"].rolling(window=7, min_periods=1).mean()
        df["rhr_90d"] = df["rhr"].rolling(window=90, min_periods=1).mean()
        df["sleep_weekly"] = df["sleep"].rolling(window=7, min_periods=1).mean()

        df_display = df[(df["date"] >= start_d) & (df["date"] <= end_d)].copy()
        if df_display.empty:
            return templates.TemplateResponse(
                "partials/recovery_trends.html",
                {
                    "request": request,
                    "has_data": False,
                    "message": "No metrics in the selected date range.",
                },
            )

        # Alert markers (fatigue flags + metric anomalies)
        triggered_days: set[date] = set()
        metric_alerts_by_day: dict[date, set[str]] = {}

        try:
            runs_all = list_recovery_alert_runs(int(target_athlete_id), start=start_d, end=end_d, limit=500)
            for r in runs_all or []:
                d = getattr(r, "alert_date", None)
                if isinstance(d, date) and bool(getattr(r, "triggered", False)):
                    triggered_days.add(d)
        except Exception:
            triggered_days = set()

        try:
            with get_session() as session:
                stmt = (
                    select(MetricAlert)
                    .where(MetricAlert.athlete_id == int(target_athlete_id))
                    .where(MetricAlert.alert_date >= start_d)
                    .where(MetricAlert.alert_date <= end_d)
                    .where(MetricAlert.severity.in_(["yellow", "red"]))
                )
                for a in session.execute(stmt).scalars().all():
                    d = getattr(a, "alert_date", None)
                    if not isinstance(d, date):
                        continue
                    metric_alerts_by_day.setdefault(d, set()).add(str(getattr(a, "metric_name", "") or ""))
        except Exception:
            metric_alerts_by_day = {}

        # Build marker payloads for the charts.
        date_to_hrv = {d: float(v) for d, v in zip(df_display["date"], df_display["hrv_7d"], strict=False)}
        date_to_rhr = {d: float(v) for d, v in zip(df_display["date"], df_display["rhr_7d"], strict=False)}

        marker_dates_all = sorted(set(metric_alerts_by_day.keys()) | set(triggered_days))

        def _marker_text(d: date, metric_filter: str | None = None) -> str:
            parts: list[str] = []
            if d in triggered_days:
                parts.append("Fatigue flag")
            metrics = metric_alerts_by_day.get(d) or set()
            if metric_filter:
                metrics = {m for m in metrics if m == metric_filter}
            if metrics:
                metrics_list = ", ".join(sorted({m for m in metrics if m}))
                if metrics_list:
                    parts.append(f"Metric alerts: {metrics_list}")
            return " • ".join(parts) if parts else "Alert"

        # HRV chart
        fig_hrv = make_subplots(specs=[[{"secondary_y": True}]])
        fig_hrv.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["hrv_90d"],
                name="HRV 90-day",
                line=dict(color="#1f77b4", width=2.8),
            ),
            secondary_y=False,
        )
        fig_hrv.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["hrv_7d"],
                name="HRV 7-day",
                line=dict(color="#a8d5ff", width=2),
            ),
            secondary_y=False,
        )
        fig_hrv.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["sleep_weekly"],
                name="Avg Sleep (weekly)",
                line=dict(color="#9467bd", width=4, dash="dash"),
            ),
            secondary_y=True,
        )

        # Red markers for fatigue flags and HRV metric alerts
        hrv_marker_dates = [d for d in marker_dates_all if d in date_to_hrv and (d in triggered_days or ("hrv" in (metric_alerts_by_day.get(d) or set())))]
        if hrv_marker_dates:
            fig_hrv.add_trace(
                go.Scatter(
                    x=hrv_marker_dates,
                    y=[date_to_hrv[d] for d in hrv_marker_dates],
                    mode="markers",
                    name="Alerts",
                    marker=dict(color="#ef4444", size=10, line=dict(color="white", width=1)),
                    text=[_marker_text(d, "hrv") for d in hrv_marker_dates],
                    hovertemplate="%{x|%Y-%m-%d}<br>%{text}<extra></extra>",
                ),
                secondary_y=False,
            )
        fig_hrv.update_xaxes(title_text="Date")
        fig_hrv.update_yaxes(title_text="HRV", secondary_y=False)
        fig_hrv.update_yaxes(title_text="Sleep Hours", secondary_y=True)
        fig_hrv.update_layout(
            title="HRV Rolling Averages with Weekly Sleep",
            hovermode="x unified",
            height=460,
            margin=dict(l=40, r=40, t=55, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )

        # RHR chart
        fig_rhr = make_subplots(specs=[[{"secondary_y": True}]])
        fig_rhr.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["rhr_90d"],
                name="RHR 90-day",
                line=dict(color="#1f77b4", width=2.8),
            ),
            secondary_y=False,
        )
        fig_rhr.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["rhr_7d"],
                name="RHR 7-day",
                line=dict(color="#a8d5ff", width=2),
            ),
            secondary_y=False,
        )
        fig_rhr.add_trace(
            go.Scatter(
                x=df_display["date"],
                y=df_display["sleep_weekly"],
                name="Avg Sleep (weekly)",
                line=dict(color="#9467bd", width=4, dash="dash"),
            ),
            secondary_y=True,
        )

        # Red markers for fatigue flags and RHR metric alerts
        rhr_marker_dates = [d for d in marker_dates_all if d in date_to_rhr and (d in triggered_days or ("rhr" in (metric_alerts_by_day.get(d) or set())))]
        if rhr_marker_dates:
            fig_rhr.add_trace(
                go.Scatter(
                    x=rhr_marker_dates,
                    y=[date_to_rhr[d] for d in rhr_marker_dates],
                    mode="markers",
                    name="Alerts",
                    marker=dict(color="#ef4444", size=10, line=dict(color="white", width=1)),
                    text=[_marker_text(d, "rhr") for d in rhr_marker_dates],
                    hovertemplate="%{x|%Y-%m-%d}<br>%{text}<extra></extra>",
                ),
                secondary_y=False,
            )
        fig_rhr.update_xaxes(title_text="Date")
        fig_rhr.update_yaxes(title_text="Resting HR (bpm)", secondary_y=False)
        fig_rhr.update_yaxes(title_text="Sleep Hours", secondary_y=True)
        fig_rhr.update_layout(
            title="Resting Heart Rate Rolling Averages with Weekly Sleep",
            hovermode="x unified",
            height=460,
            margin=dict(l=40, r=40, t=55, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )

        return templates.TemplateResponse(
            "partials/recovery_trends.html",
            {
                "request": request,
                "has_data": True,
                # Use Plotly's JSON serializer to handle numpy/pandas types.
                "fig_hrv_json": fig_hrv.to_json(),
                "fig_rhr_json": fig_rhr.to_json(),
            },
        )

    @app.get("/partials/recovery_summary", response_class=HTMLResponse)
    def partial_recovery_summary(
        request: Request,
        target_athlete_id: int,
        end: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        end_d = _parse_date(end) or get_effective_today()

        def _select_baseline_mean(athlete_id: int, metric_name: str) -> tuple[float | None, str | None]:
            # Prefer monthly baseline (stable), fall back to longer windows if needed.
            for window in ("monthly", "quarterly", "semiannual", "annual"):
                b = get_baseline_asof(int(athlete_id), metric_name, window, end_d)
                if b and b.mean is not None:
                    return float(b.mean), window
            return None, None

        with get_session() as session:
            # "Current" is the most recent metric at or before the selected end date.
            metric = session.execute(
                select(DailyMetric)
                .where(DailyMetric.athlete_id == int(target_athlete_id))
                .where(DailyMetric.date <= end_d)
                .order_by(DailyMetric.date.desc())
            ).scalars().first()

        hrv_base, hrv_window = _select_baseline_mean(int(target_athlete_id), "hrv")
        sleep_base, sleep_window = _select_baseline_mean(int(target_athlete_id), "sleep_hours")
        rhr_base, rhr_window = _select_baseline_mean(int(target_athlete_id), "rhr")

        summary = {
            "as_of": metric.date if metric else None,
            "hrv": {
                "baseline": hrv_base,
                "baseline_window": hrv_window,
                "current": float(metric.hrv) if metric and metric.hrv is not None else None,
            },
            "sleep": {
                "baseline": sleep_base,
                "baseline_window": sleep_window,
                "current": float(metric.sleep_hours) if metric and metric.sleep_hours is not None else None,
            },
            "rhr": {
                "baseline": rhr_base,
                "baseline_window": rhr_window,
                "current": float(metric.rhr) if metric and metric.rhr is not None else None,
            },
        }

        return templates.TemplateResponse(
            "partials/recovery_summary.html",
            {
                "request": request,
                "end": end_d,
                "summary": summary,
            },
        )

    @app.get("/partials/race_performance", response_class=HTMLResponse)
    def partial_race_performance(
        request: Request,
        target_athlete_id: int,
        race_year: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        races_all = load_local_race_results(int(target_athlete_id))
        years_with_data = sorted(
            {
                int(r["event_date"].year)
                for r in races_all
                if isinstance(r.get("event_date"), date)
            },
            reverse=True,
        )
        years = sorted(set(years_with_data).union({date.today().year}), reverse=True)

        if not years_with_data:
            return templates.TemplateResponse(
                "partials/race_performance.html",
                {
                    "request": request,
                    "has_data": False,
                    "message": "No local race results found for this athlete yet. Run a race sync and mapping first.",
                    "years": [],
                    "selected_year": None,
                    "race_count": 0,
                    "finished_count": None,
                    "best_race": None,
                    "worst_finished": None,
                    "fig_json": None,
                },
            )

        default_year = years_with_data[0]
        selected_year: int
        try:
            selected_year = int(race_year) if race_year is not None and str(race_year).strip() else default_year
        except Exception:
            selected_year = default_year
        if selected_year not in years:
            selected_year = default_year

        races_year = [
            r
            for r in races_all
            if isinstance(r.get("event_date"), date) and int(r["event_date"].year) == int(selected_year)
        ]
        races_year_sorted = sorted(races_year, key=lambda r: r.get("event_date") or date.min)

        # Default comparison choices: most recent two races in the selected year.
        races_year_by_recent = sorted(races_year_sorted, key=lambda r: r.get("event_date") or date.min, reverse=True)

        def _race_key(r: dict) -> str | None:
            eid = r.get("event_id")
            pid = r.get("prog_id")
            if not isinstance(eid, int) or not isinstance(pid, int):
                return None
            return f"{int(eid)}:{int(pid)}"

        default_a = _race_key(races_year_by_recent[0]) if len(races_year_by_recent) >= 1 else None
        default_b = _race_key(races_year_by_recent[1]) if len(races_year_by_recent) >= 2 else None

        best_worst = pick_best_worst(races_year_sorted)
        best_race = best_worst.get("best_finished")
        worst_finished = best_worst.get("worst_finished")

        finished_count = sum(
            1
            for r in races_year_sorted
            if (r.get("finish_status") == "FINISH") and isinstance(r.get("finish_position"), int)
        )

        # Chart: plot finished placings as a line, and non-finishes as red X markers at the bottom.
        xs_finish: list[date] = []
        ys_finish: list[int] = []
        finish_names: list[str] = []
        hover_finish: list[str] = []

        xs_nf: list[date] = []
        ys_nf: list[int] = []
        hover_nf: list[str] = []

        def _short_for_annotation(value: str, max_len: int = 34) -> str:
            s = " ".join((value or "").split())
            if len(s) <= max_len:
                return s
            return s[: max_len - 1] + "…"

        def _format_hover(name: str, prog_name: str | None, status: str | None, placing: int | None) -> str:
            st = (status or "FINISH").strip().upper() or "FINISH"
            prog = (prog_name or "").strip()
            prog_line = f"{prog}<br>" if prog else ""
            place_str = str(placing) if placing is not None else "—"
            return f"{name}<br>{prog_line}{st} ● {place_str}"

        for r in races_year_sorted:
            d = r.get("event_date")
            if not isinstance(d, date):
                continue
            name = str(r.get("event_name") or "Race")
            status = str(r.get("finish_status") or "")
            prog_name = r.get("prog_name")
            pos = r.get("finish_position")

            is_finished = (status.strip().upper() == "FINISH") and isinstance(pos, int)
            if is_finished:
                xs_finish.append(d)
                ys_finish.append(int(pos))
                finish_names.append(name)
                hover_finish.append(_format_hover(name, str(prog_name) if prog_name is not None else None, status, int(pos)))
            else:
                # Non-finish or no numeric placing: still show the race occurrence.
                xs_nf.append(d)
                hover_nf.append(_format_hover(name, str(prog_name) if prog_name is not None else None, status, None))

        fig_json: str | None = None
        if xs_finish or xs_nf:
            import plotly.graph_objects as go

            fig = go.Figure()
            if xs_finish:
                fig.add_trace(
                    go.Scatter(
                        x=xs_finish,
                        y=ys_finish,
                        mode="lines+markers",
                        hovertext=hover_finish,
                        hoverinfo="text",
                        line=dict(color="#2563eb", width=3),
                        marker=dict(color="#2563eb", size=10),
                        showlegend=False,
                    )
                )

            # Place non-finish markers below the worst placing so they appear at the bottom.
            if xs_nf:
                bottom_y = (max(ys_finish) if ys_finish else 1) + 3
                ys_nf = [bottom_y for _ in xs_nf]
                fig.add_trace(
                    go.Scatter(
                        x=xs_nf,
                        y=ys_nf,
                        mode="markers",
                        hovertext=hover_nf,
                        hoverinfo="text",
                        marker=dict(color="#dc2626", size=12, symbol="x"),
                        showlegend=False,
                    )
                )

            # Season best: smallest placing number.
            try:
                best_i = min(range(len(ys_finish)), key=lambda i: ys_finish[i])
            except ValueError:
                best_i = None
            if best_i is not None and 0 <= best_i < len(xs_finish):
                best_x = xs_finish[best_i]
                best_y = ys_finish[best_i]
                best_name = finish_names[best_i] if best_i < len(finish_names) else "Race"
                fig.add_trace(
                    go.Scatter(
                        x=[best_x],
                        y=[best_y],
                        mode="markers",
                        marker=dict(color="#16a34a", size=14, symbol="star"),
                        hovertext=[f"Season Best<br>{best_name}<br>FINISH ● {best_y}"],
                        hoverinfo="text",
                        showlegend=False,
                    )
                )
                fig.add_annotation(
                    x=best_x,
                    y=best_y,
                    text=f"Season best: {best_y} ({_short_for_annotation(best_name)})",
                    showarrow=True,
                    arrowhead=2,
                    ax=0,
                    ay=-40,
                    bgcolor="rgba(220,252,231,0.85)",
                    bordercolor="#16a34a",
                    borderwidth=1,
                    font=dict(color="#166534"),
                )

            fig.update_xaxes(title_text="Race Date")
            fig.update_yaxes(title_text="Finish Position", autorange="reversed")
            fig.update_layout(
                height=440,
                margin=dict(l=40, r=40, t=55, b=40),
                showlegend=False,
            )
            fig_json = fig.to_json()

        return templates.TemplateResponse(
            "partials/race_performance.html",
            {
                "request": request,
                "has_data": True,
                "message": None,
                "years": years,
                "selected_year": selected_year,
                "races": races_year_by_recent,
                "default_compare_a": default_a,
                "default_compare_b": default_b,
                "race_count": len(races_year_sorted),
                "finished_count": finished_count,
                "best_race": best_race,
                "worst_finished": worst_finished,
                "fig_json": fig_json,
            },
        )

    def _parse_event_prog_key(value: str | None) -> tuple[int, int] | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if ":" not in s:
            return None
        a, b = s.split(":", 1)
        try:
            return int(a), int(b)
        except Exception:
            return None

    @app.get("/partials/race_comparison", response_class=HTMLResponse)
    def partial_race_comparison(
        request: Request,
        target_athlete_id: int,
        compare_race_a: str | None = None,
        compare_race_b: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        key_a = _parse_event_prog_key(compare_race_a)
        key_b = _parse_event_prog_key(compare_race_b)
        if not key_a or not key_b:
            return templates.TemplateResponse(
                "partials/race_comparison.html",
                {"request": request, "ready": False, "error": None},
            )
        if key_a == key_b:
            return templates.TemplateResponse(
                "partials/race_comparison.html",
                {"request": request, "ready": True, "error": "Choose two different races."},
            )

        event_id_a, prog_id_a = key_a
        event_id_b, prog_id_b = key_b

        with get_session() as session:
            race_a = (
                session.execute(
                    select(WTORaceResult)
                    .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                    .where(WTORaceResult.event_id == int(event_id_a))
                    .where(WTORaceResult.prog_id == int(prog_id_a))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            race_b = (
                session.execute(
                    select(WTORaceResult)
                    .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                    .where(WTORaceResult.event_id == int(event_id_b))
                    .where(WTORaceResult.prog_id == int(prog_id_b))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            mapping = (
                session.execute(
                    select(WTODashboardAthleteMap).where(WTODashboardAthleteMap.podium_athlete_id == int(target_athlete_id))
                )
                .scalars()
                .first()
            )

        if not race_a or not race_b:
            return templates.TemplateResponse(
                "partials/race_comparison.html",
                {"request": request, "ready": True, "error": "Race selection not found in local cache. Try syncing races again."},
            )
        if not mapping or not mapping.wto_athlete_id:
            return templates.TemplateResponse(
                "partials/race_comparison.html",
                {"request": request, "ready": True, "error": "This athlete is not mapped to a WTO athlete yet."},
            )

        tri_engine = get_triathlon_engine()
        if tri_engine is None:
            return templates.TemplateResponse(
                "partials/race_comparison.html",
                {"request": request, "ready": True, "error": "TRIATHLON_DATABASE_URL is not configured on this server."},
            )

        wto_athlete_id = int(mapping.wto_athlete_id)

        def _fetch_position_metrics(event_id: int, prog_id: int) -> dict:
            sql = text(
                """
                SELECT
                    swimrank,
                    t1rank,
                    bikerank,
                    t2rank,
                    runrank,
                    position_at_swim,
                    position_at_t1,
                    position_at_bike,
                    position_at_t2,
                    position_at_run,
                    behindswim,
                    behindt1,
                    behindbike,
                    behindt2,
                    behindrun
                FROM position_metrics
                WHERE event_id = :event_id
                  AND prog_id = :prog_id
                  AND athlete_id = :athlete_id
                LIMIT 1
                """
            )
            with tri_engine.connect() as conn:
                row = conn.execute(
                    sql,
                    {"event_id": int(event_id), "prog_id": int(prog_id), "athlete_id": int(wto_athlete_id)},
                ).mappings().fetchone()
            return dict(row) if row else {}

        m_a = _fetch_position_metrics(event_id_a, prog_id_a)
        m_b = _fetch_position_metrics(event_id_b, prog_id_b)

        def _val_or_dash(v: object) -> str:
            if v is None:
                return "—"
            if isinstance(v, str) and not v.strip():
                return "—"
            try:
                if isinstance(v, float) and (v != v):
                    return "—"
            except Exception:
                pass
            return str(v)

        def _time_to_seconds(v: object) -> int | None:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                try:
                    sec = int(v)
                    return sec if sec >= 0 else None
                except Exception:
                    return None
            if not isinstance(v, str):
                return None

            s = v.strip()
            if not s or s == "—":
                return None
            # Accept HH:MM:SS, MM:SS, or SS (optionally with fractional seconds)
            parts = s.split(":")
            try:
                if len(parts) == 3:
                    h = int(parts[0])
                    m = int(parts[1])
                    sec = int(float(parts[2]))
                    return h * 3600 + m * 60 + sec
                if len(parts) == 2:
                    m = int(parts[0])
                    sec = int(float(parts[1]))
                    return m * 60 + sec
                if len(parts) == 1:
                    return int(float(parts[0]))
            except Exception:
                return None
            return None

        def _faster_time_class(a: object, b: object) -> tuple[str, str]:
            """Return CSS classes for (a,b) where lower duration is better."""
            as_ = _time_to_seconds(a)
            bs_ = _time_to_seconds(b)
            if as_ is None or bs_ is None:
                return "", ""
            if as_ < bs_:
                return "cell-better", ""
            if bs_ < as_:
                return "", "cell-better"
            return "", ""

        def _better_class(a: object, b: object, *, invert: bool = False) -> tuple[str, str]:
            """Return CSS classes for (a,b). Lower is better by default."""
            try:
                ai = int(a) if a is not None else None
            except Exception:
                ai = None
            try:
                bi = int(b) if b is not None else None
            except Exception:
                bi = None

            if ai is None or bi is None:
                return "", ""
            # For these metrics, lower is better.
            if invert:
                ai, bi = -ai, -bi
            if ai < bi:
                return "cell-better", ""
            if bi < ai:
                return "", "cell-better"
            return "", ""

        rank_defs = [
            ("Swim", "swimrank", "swim_time"),
            ("T1", "t1rank", "t1_time"),
            ("Bike", "bikerank", "bike_time"),
            ("T2", "t2rank", "t2_time"),
            ("Run", "runrank", "run_time"),
        ]
        rank_rows: list[dict] = []
        for label, rank_key, time_attr in rank_defs:
            a_val = m_a.get(rank_key)
            b_val = m_b.get(rank_key)
            a_class, b_class = _better_class(a_val, b_val)

            a_time_raw = getattr(race_a, time_attr, None)
            b_time_raw = getattr(race_b, time_attr, None)
            a_time_class, b_time_class = _faster_time_class(a_time_raw, b_time_raw)
            rank_rows.append(
                {
                    "label": label,
                    "a_val": _val_or_dash(a_val),
                    "b_val": _val_or_dash(b_val),
                    "a_class": a_class,
                    "b_class": b_class,
                    "a_time": _val_or_dash(a_time_raw),
                    "b_time": _val_or_dash(b_time_raw),
                    "a_time_class": a_time_class,
                    "b_time_class": b_time_class,
                }
            )

        # Build charts (checkpoint placing and gap to leader)
        checkpoints = ["Swim", "T1", "Bike", "T2", "Run"]

        def _int_or_none(v: object) -> int | None:
            try:
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        place_a = [
            _int_or_none(m_a.get("position_at_swim")),
            _int_or_none(m_a.get("position_at_t1")),
            _int_or_none(m_a.get("position_at_bike")),
            _int_or_none(m_a.get("position_at_t2")),
            _int_or_none(m_a.get("position_at_run")),
        ]
        place_b = [
            _int_or_none(m_b.get("position_at_swim")),
            _int_or_none(m_b.get("position_at_t1")),
            _int_or_none(m_b.get("position_at_bike")),
            _int_or_none(m_b.get("position_at_t2")),
            _int_or_none(m_b.get("position_at_run")),
        ]

        gap_a = [
            _int_or_none(m_a.get("behindswim")),
            _int_or_none(m_a.get("behindt1")),
            _int_or_none(m_a.get("behindbike")),
            _int_or_none(m_a.get("behindt2")),
            _int_or_none(m_a.get("behindrun")),
        ]
        gap_b = [
            _int_or_none(m_b.get("behindswim")),
            _int_or_none(m_b.get("behindt1")),
            _int_or_none(m_b.get("behindbike")),
            _int_or_none(m_b.get("behindt2")),
            _int_or_none(m_b.get("behindrun")),
        ]

        fig_place_json: str | None = None
        fig_gap_json: str | None = None

        import plotly.graph_objects as go

        if any(v is not None for v in place_a) or any(v is not None for v in place_b):
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=checkpoints,
                    y=place_a,
                    mode="lines+markers",
                    name="Race A",
                    line=dict(color="#dc2626", width=3),
                    marker=dict(color="#dc2626", size=10),
                    hovertemplate="%{x}<br>Race A ● %{y}<extra></extra>",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=checkpoints,
                    y=place_b,
                    mode="lines+markers",
                    name="Race B",
                    line=dict(color="#2563eb", width=3),
                    marker=dict(color="#2563eb", size=10),
                    hovertemplate="%{x}<br>Race B ● %{y}<extra></extra>",
                )
            )
            place_vals = [v for v in (place_a + place_b) if isinstance(v, int)]
            y_bottom = max(place_vals) if place_vals else 1
            y_bottom = max(int(y_bottom), 1)
            # Add padding so a place of 1 isn't clipped at the top edge.
            y_top = 0.5
            fig.update_yaxes(
                title_text="Place",
                autorange=False,
                range=[y_bottom + 1, y_top],
            )
            fig.update_layout(height=360, margin=dict(l=40, r=20, t=20, b=40), legend=dict(orientation="h"))
            fig_place_json = fig.to_json()

        if any(v is not None for v in gap_a) or any(v is not None for v in gap_b):
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=checkpoints,
                    y=gap_a,
                    mode="lines+markers",
                    name="Race A",
                    line=dict(color="#dc2626", width=3),
                    marker=dict(color="#dc2626", size=10),
                    hovertemplate="%{x}<br>Race A ● %{y}s<extra></extra>",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=checkpoints,
                    y=gap_b,
                    mode="lines+markers",
                    name="Race B",
                    line=dict(color="#2563eb", width=3),
                    marker=dict(color="#2563eb", size=10),
                    hovertemplate="%{x}<br>Race B ● %{y}s<extra></extra>",
                )
            )
            # Add padding so 0s isn't clipped at the bottom edge.
            gap_vals = [v for v in (gap_a + gap_b) if isinstance(v, int)]
            max_gap = max(gap_vals) if gap_vals else 0
            pad = max(1, int(round(max_gap * 0.08)))
            fig.update_yaxes(
                title_text="Seconds Behind Leader",
                autorange=False,
                range=[-pad, max_gap + pad],
            )
            fig.update_layout(height=360, margin=dict(l=50, r=20, t=20, b=40), legend=dict(orientation="h"))
            fig_gap_json = fig.to_json()

        def _race_summary(r: WTORaceResult) -> dict:
            return {
                "event_date": r.event_date,
                "event_name": r.event_name or "Race",
                "prog_name": r.prog_name,
                "finish_status": getattr(r, "finish_status", None),
                "finish_position": getattr(r, "finish_position", None),
                "total_time": getattr(r, "total_time", None),
            }

        overall_a_class, overall_b_class = _better_class(
            getattr(race_a, "finish_position", None),
            getattr(race_b, "finish_position", None),
        )

        return templates.TemplateResponse(
            "partials/race_comparison.html",
            {
                "request": request,
                "ready": True,
                "error": None,
                "race_a": _race_summary(race_a),
                "race_b": _race_summary(race_b),
                "rank_rows": rank_rows,
                "fig_place_json": fig_place_json,
                "fig_gap_json": fig_gap_json,
                "overall_a_class": overall_a_class,
                "overall_b_class": overall_b_class,
            },
        )

    def _sport_norm(value: str | None) -> str:
        return (value or "").strip().lower()

    def _sport_matches(workout_sport: str | None, desired: str) -> bool:
        ws = _sport_norm(workout_sport)
        d = _sport_norm(desired)
        if not d:
            return True
        if d == "run":
            return ws in {"run", "running"}
        if d == "bike":
            return ws in {"bike", "cycling", "ride"}
        if d == "swim":
            return ws in {"swim", "swimming"}
        return ws == d

    def _format_duration(seconds: int | None) -> str:
        if not isinstance(seconds, int) or seconds <= 0:
            return "—"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @app.get("/partials/race_tp_compare", response_class=HTMLResponse)
    def partial_race_tp_compare(
        request: Request,
        target_athlete_id: int,
        compare_race_a: str | None = None,
        compare_race_b: str | None = None,
        tp_compare_sport: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        key_a = _parse_event_prog_key(compare_race_a)
        key_b = _parse_event_prog_key(compare_race_b)
        if not key_a or not key_b:
            return templates.TemplateResponse(
                "partials/race_tp_compare.html",
                {"request": request, "error": "Select Race A and Race B above to load TrainingPeaks options."},
            )

        selected_sport = _sport_norm(tp_compare_sport) or "run"
        sport_options = [
            {"value": "run", "label": "Run"},
            {"value": "bike", "label": "Bike"},
            {"value": "swim", "label": "Swim"},
        ]

        (event_id_a, prog_id_a) = key_a
        (event_id_b, prog_id_b) = key_b

        with get_session() as session:
            race_a = (
                session.execute(
                    select(WTORaceResult)
                    .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                    .where(WTORaceResult.event_id == int(event_id_a))
                    .where(WTORaceResult.prog_id == int(prog_id_a))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            race_b = (
                session.execute(
                    select(WTORaceResult)
                    .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                    .where(WTORaceResult.event_id == int(event_id_b))
                    .where(WTORaceResult.prog_id == int(prog_id_b))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            athlete = session.get(Athlete, int(target_athlete_id))

        if not race_a or not race_b:
            return templates.TemplateResponse(
                "partials/race_tp_compare.html",
                {"request": request, "error": "Race selection not found in local cache."},
            )

        has_tp = athlete and athlete.tp_athlete_id
        api = get_api(int(target_athlete_id)) if has_tp else None

        def _manual_workouts() -> list[dict]:
            """Return all manually-uploaded FIT workouts (tp_workout_id starts with 'manual_')."""
            with get_session() as s:
                rows = s.execute(
                    select(Workout)
                    .where(Workout.athlete_id == int(target_athlete_id))
                    .where(Workout.tp_workout_id.like("manual_%"))
                    .order_by(Workout.date.desc())
                ).scalars().all()
            out: list[dict] = []
            for w in rows:
                sport_str = str(w.sport or "")
                dur_str = _format_duration(int(w.duration_sec)) if w.duration_sec else ""
                raw = w.raw_json or {}
                label_parts = [f"📎 {w.date}", sport_str, str(raw.get("label") or "Uploaded FIT")]
                if dur_str:
                    label_parts.append(dur_str)
                out.append({"workout_id": str(w.tp_workout_id), "sport": sport_str, "duration_sec": w.duration_sec, "label": " • ".join(p for p in label_parts if p)})
            return out

        def _tp_workouts_for_day(d: date) -> list[dict]:
            start = d - timedelta(days=1)
            end = d + timedelta(days=1)
            manual = _manual_workouts()
            if not has_tp:
                return manual
            try:
                workouts = api.fetch_workouts(start, end, tp_athlete_id=int(athlete.tp_athlete_id))
            except Exception as e:  # noqa: BLE001
                return manual or [{"workout_id": "", "label": f"(Error fetching workouts: {e})"}]

            out: list[dict] = []
            for w in workouts or []:
                wid = w.get("workoutId") or w.get("WorkoutId") or w.get("id") or w.get("Id")
                if not wid:
                    continue
                sport = w.get("WorkoutType") or w.get("sportType") or w.get("sport") or ""
                title = w.get("Title") or w.get("title") or w.get("WorkoutName") or ""
                date_field = w.get("workoutDay") or w.get("WorkoutDay") or w.get("Date") or w.get("date")
                w_day = _coerce_date(date_field)

                completed = bool(w.get("Completed", False))
                dur_sec = None
                if completed and w.get("TotalTime"):
                    try:
                        val = float(w.get("TotalTime"))
                        dur_sec = int(val * 3600) if val < 20 else int(val)
                    except Exception:
                        dur_sec = None

                tss_val = w.get("TssActual") if completed else None
                if_val = w.get("IF") if completed else None

                parts = []
                if w_day:
                    parts.append(str(w_day))
                if sport:
                    parts.append(str(sport))
                if title:
                    parts.append(str(title))
                meta = []
                if dur_sec:
                    meta.append(_format_duration(int(dur_sec)))
                if tss_val is not None:
                    try:
                        meta.append(f"TSS {float(tss_val):.0f}")
                    except Exception:
                        pass
                if if_val is not None:
                    try:
                        meta.append(f"IF {float(if_val):.2f}")
                    except Exception:
                        pass

                label = " • ".join([p for p in parts if p])
                if meta:
                    label = f"{label} — {', '.join(meta)}"
                out.append({"workout_id": str(wid), "sport": str(sport or ""), "duration_sec": dur_sec, "label": label})

            # Include manually-uploaded FIT workouts (prepend so they always appear)
            out = manual + out
            # Prefer matches first, then longer workouts (manual uploads sort last by label prefix).
            out.sort(
                key=lambda x: (
                    0 if _sport_matches(x.get("sport"), selected_sport) else 1,
                    -(int(x.get("duration_sec") or 0)),
                    x.get("label") or "",
                )
            )
            return out

        workouts_a = _tp_workouts_for_day(race_a.event_date)
        workouts_b = _tp_workouts_for_day(race_b.event_date)

        def _pick_default(workouts: list[dict]) -> str | None:
            for w in workouts:
                if w.get("workout_id") and _sport_matches(w.get("sport"), selected_sport):
                    return str(w["workout_id"])
            for w in workouts:
                if w.get("workout_id"):
                    return str(w["workout_id"])
            return None

        default_a = _pick_default(workouts_a)
        default_b = _pick_default(workouts_b)

        # Auto-cache: pre-fetch timeseries for default race-day workouts in background.
        # This runs inline (blocking) on the compare endpoint so the trace endpoint
        # can serve instantly. Only caches if not already cached.
        import threading

        def _precache_workout(wid: str):
            if not wid or is_timeseries_cached(wid):
                return
            if not has_tp or not athlete.tp_athlete_id:
                return  # manual uploads are already cached; skip TP fetch for non-TP athletes
            try:
                fetch_timeseries_cached(api, wid, int(athlete.tp_athlete_id))
            except Exception:
                pass  # Non-critical; trace endpoint will retry if needed

        # Fire in background threads to avoid blocking the compare response
        for wid in (default_a, default_b):
            if wid and not is_timeseries_cached(wid):
                t = threading.Thread(target=_precache_workout, args=(wid,), daemon=True)
                t.start()

        return templates.TemplateResponse(
            "partials/race_tp_compare.html",
            {
                "request": request,
                "error": None,
                "sport_options": sport_options,
                "selected_sport": selected_sport,
                "workouts_a": workouts_a,
                "workouts_b": workouts_b,
                "selected_workout_a": default_a,
                "selected_workout_b": default_b,
            },
        )

    def _persist_workout_summaries(tp_workout_id: str, payload: dict):
        """Save compact WorkoutStats + LapStats to DB tables (upsert).

        Non-critical helper — caller should catch exceptions.
        """
        summary = extract_workout_summary(payload)
        laps = extract_lap_summaries(payload)

        with get_session() as session:
            # Find the local Workout row by tp_workout_id
            workout = session.execute(
                select(Workout).where(Workout.tp_workout_id == str(tp_workout_id)).limit(1)
            ).scalars().first()
            workout_db_id = workout.id if workout else None

            # Upsert WorkoutDetail
            existing = None
            if workout_db_id:
                existing = session.execute(
                    select(WorkoutDetail).where(WorkoutDetail.workout_id == workout_db_id).limit(1)
                ).scalars().first()

            if existing:
                # Update existing row
                for key, val in summary.items():
                    if val is not None:
                        col_name = key  # summary keys match column names
                        if hasattr(existing, col_name):
                            setattr(existing, col_name, val)
                existing.timeseries_cached_at = datetime.now(tz=None)
            elif workout_db_id:
                detail = WorkoutDetail(
                    workout_id=workout_db_id,
                    tp_workout_id=str(tp_workout_id),
                    workout_name=summary.get("name"),
                    elapsed_time_ms=summary.get("elapsed_time_ms"),
                    tss=summary.get("tss"),
                    intensity_factor=summary.get("intensity_factor"),
                    normalized_power=summary.get("normalized_power"),
                    power_average=summary.get("power_average"),
                    power_maximum=summary.get("power_maximum"),
                    hr_average=summary.get("hr_average"),
                    hr_maximum=summary.get("hr_maximum"),
                    hr_minimum=summary.get("hr_minimum"),
                    speed_average=summary.get("speed_average"),
                    speed_maximum=summary.get("speed_maximum"),
                    normalized_speed=summary.get("normalized_speed"),
                    cadence_average=summary.get("cadence_average"),
                    cadence_maximum=summary.get("cadence_maximum"),
                    energy_kj=summary.get("energy_kj"),
                    elevation_gain=summary.get("elevation_gain"),
                    elevation_loss=summary.get("elevation_loss"),
                    watts_per_kg=summary.get("watts_per_kg"),
                    efficiency_factor=summary.get("efficiency_factor"),
                    power_pulse_decoupling=summary.get("power_pulse_decoupling"),
                    speed_pulse_decoupling=summary.get("speed_pulse_decoupling"),
                    vi=summary.get("vi"),
                    timeseries_cached_at=datetime.now(tz=None),
                )
                session.add(detail)

            # Upsert WorkoutLaps
            if workout_db_id and laps:
                # Delete existing laps for this workout, then re-insert
                session.execute(
                    delete(WorkoutLap).where(WorkoutLap.workout_id == workout_db_id)
                )
                for lap_data in laps:
                    lap = WorkoutLap(
                        workout_id=workout_db_id,
                        lap_number=lap_data["lap_number"],
                        lap_name=lap_data.get("name"),
                        start_time_ms=lap_data.get("start_time_ms"),
                        end_time_ms=lap_data.get("end_time_ms"),
                        elapsed_time_ms=lap_data.get("elapsed_time_ms"),
                        tss=lap_data.get("tss"),
                        intensity_factor=lap_data.get("intensity_factor"),
                        normalized_power=lap_data.get("normalized_power"),
                        power_average=lap_data.get("power_average"),
                        power_maximum=lap_data.get("power_maximum"),
                        hr_average=lap_data.get("hr_average"),
                        hr_maximum=lap_data.get("hr_maximum"),
                        hr_minimum=lap_data.get("hr_minimum"),
                        speed_average=lap_data.get("speed_average"),
                        speed_maximum=lap_data.get("speed_maximum"),
                        cadence_average=lap_data.get("cadence_average"),
                        energy_kj=lap_data.get("energy_kj"),
                        elevation_gain=lap_data.get("elevation_gain"),
                        watts_per_kg=lap_data.get("watts_per_kg"),
                    )
                    session.add(lap)

            session.commit()

    def _extract_channels_payload(payload: dict) -> tuple[list[str], list[list[object]]]:
        """Normalize TP timeseries into (channels, rows) handling both formats.

        Standard TP API returns Data as dicts: {Event, MillisecondOffset, Values: [...]}
        Legacy/other format may return flat list rows.
        Uses the shared normalize_timeseries_rows() from workout_cache.
        """
        return normalize_timeseries_rows(payload)

    def _find_channel_index(channels: list[str], candidates: set[str]) -> int | None:
        if not channels:
            return None
        for i, name in enumerate(channels):
            n = (name or "").strip().lower().replace(" ", "")
            if n in candidates:
                return i
        # fallback substring match
        for i, name in enumerate(channels):
            n = (name or "").strip().lower().replace(" ", "")
            for c in candidates:
                if c in n:
                    return i
        return None

    @app.get("/partials/race_tp_traces", response_class=HTMLResponse)
    def partial_race_tp_traces(
        request: Request,
        target_athlete_id: int,
        tp_compare_sport: str | None = None,
        tp_workout_a: str | None = None,
        tp_workout_b: str | None = None,
        compare_race_a: str | None = None,
        compare_race_b: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        sport = _sport_norm(tp_compare_sport) or "run"
        wid_a = (tp_workout_a or "").strip()
        wid_b = (tp_workout_b or "").strip()
        if not wid_a or not wid_b:
            return templates.TemplateResponse(
                "partials/race_tp_traces.html",
                {"request": request, "error": "Select both workouts first.", "fig_json": None, "title": "", "notes": None},
            )

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))

        all_manual = wid_a.startswith("manual_") and wid_b.startswith("manual_")
        has_tp = athlete and athlete.tp_athlete_id
        if not all_manual and not has_tp:
            return templates.TemplateResponse(
                "partials/race_tp_traces.html",
                {"request": request, "error": "Missing tp_athlete_id for this athlete. Upload FIT files manually instead.", "fig_json": None, "title": "", "notes": None},
            )

        api = get_api(int(target_athlete_id)) if has_tp else None
        tp_aid = int(athlete.tp_athlete_id) if has_tp else 0
        cache_note_parts: list[str] = []
        try:
            from app.services.workout_cache import get_cached_timeseries

            def _load_timeseries(wid: str) -> dict:
                # For manually uploaded FIT, always use disk cache (no API needed)
                if wid.startswith("manual_"):
                    cached = get_cached_timeseries(wid)
                    if cached is None:
                        raise RuntimeError(f"Uploaded workout not found in cache ({wid}). Please re-upload the FIT file.")
                    return cached
                return fetch_timeseries_cached(api, wid, tp_aid)

            # Cache-through: check disk cache first, fetch from API only on miss
            a_cached = is_timeseries_cached(wid_a)
            b_cached = is_timeseries_cached(wid_b)
            data_a = _load_timeseries(wid_a)
            data_b = _load_timeseries(wid_b)
            if a_cached and b_cached:
                cache_note_parts.append("Both workouts loaded from cache (instant).")
            elif a_cached or b_cached:
                cache_note_parts.append("One workout loaded from cache; other fetched from TP API and cached.")
            else:
                cache_note_parts.append("Both workouts fetched from TP API and cached for next time.")
        except Exception as e:  # noqa: BLE001
            return templates.TemplateResponse(
                "partials/race_tp_traces.html",
                {"request": request, "error": str(e), "fig_json": None, "title": "", "notes": None},
            )

        # Persist compact summaries to DB (fire-and-forget, don't block render)
        try:
            _persist_workout_summaries(wid_a, data_a)
            _persist_workout_summaries(wid_b, data_b)
        except Exception:  # noqa: BLE001
            pass  # Non-critical; summaries are a bonus

        def _series(payload: dict) -> dict:
            channels, rows = _extract_channels_payload(payload)
            n = len(rows)
            if n == 0:
                return {"x_min": [], "speed": [], "hr": [], "power": []}

            idx_time = _find_channel_index(channels, {"millisecondoffset", "time", "seconds", "sec", "elapsedtime", "elapsedseconds"})
            idx_speed = _find_channel_index(channels, {"speed", "velocity", "vel"})
            idx_hr = _find_channel_index(channels, {"heartrate", "hr"})
            idx_power = _find_channel_index(channels, {"power", "watts", "w"})

            # Detect if time channel is in milliseconds (MillisecondOffset) vs seconds
            time_is_ms = False
            if idx_time is not None:
                ch_name = (channels[idx_time] or "").strip().lower().replace(" ", "")
                time_is_ms = "millisecond" in ch_name

            max_points = 1400
            stride = max(1, int(math.ceil(n / max_points)))

            xs: list[float] = []
            speed: list[float | None] = []
            hr: list[float | None] = []
            power: list[float | None] = []

            for i in range(0, n, stride):
                r = rows[i]
                t_sec: float
                if idx_time is not None and idx_time < len(r):
                    try:
                        raw_t = float(r[idx_time])
                        t_sec = raw_t / 1000.0 if time_is_ms else raw_t
                    except Exception:
                        t_sec = float(i)
                else:
                    t_sec = float(i)
                xs.append(t_sec / 60.0)

                def _get(idx: int | None) -> float | None:
                    if idx is None or idx >= len(r):
                        return None
                    try:
                        v = r[idx]
                        if v is None:
                            return None
                        return float(v)
                    except Exception:
                        return None

                speed.append(_get(idx_speed))
                hr.append(_get(idx_hr))
                power.append(_get(idx_power))

            return {"x_min": xs, "speed": speed, "hr": hr, "power": power}

        s_a = _series(data_a)
        s_b = _series(data_b)

        import plotly.graph_objects as go

        fig = go.Figure()
        notes: str | None = None

        def _pace_min_per_mile(speed_mps: float | None) -> float | None:
            if speed_mps is None or speed_mps <= 0:
                return None
            sec_per_mile = 1609.34 / speed_mps
            return sec_per_mile / 60.0

        def _pace_min_per_100m(speed_mps: float | None) -> float | None:
            if speed_mps is None or speed_mps <= 0:
                return None
            sec_per_100m = 100.0 / speed_mps
            return sec_per_100m / 60.0

        title = ""
        if sport == "run":
            title = "Run: Pace + Heart Rate"
            y_a = [_pace_min_per_mile(v) for v in s_a["speed"]]
            y_b = [_pace_min_per_mile(v) for v in s_b["speed"]]
            fig.add_trace(go.Scatter(x=s_a["x_min"], y=y_a, mode="lines", name="Race A Pace", line=dict(color="#dc2626", width=2)))
            fig.add_trace(go.Scatter(x=s_b["x_min"], y=y_b, mode="lines", name="Race B Pace", line=dict(color="#2563eb", width=2)))

            if any(v is not None for v in s_a["hr"]) or any(v is not None for v in s_b["hr"]):
                fig.add_trace(go.Scatter(x=s_a["x_min"], y=s_a["hr"], mode="lines", name="Race A HR", line=dict(color="#dc2626", width=1, dash="dot"), yaxis="y2"))
                fig.add_trace(go.Scatter(x=s_b["x_min"], y=s_b["hr"], mode="lines", name="Race B HR", line=dict(color="#2563eb", width=1, dash="dot"), yaxis="y2"))
                fig.update_layout(yaxis2=dict(title="Heart Rate (bpm)", overlaying="y", side="right"))
            fig.update_yaxes(title_text="Pace (min/mi)")

        elif sport == "bike":
            title = "Bike: Power + Heart Rate"
            if any(v is not None for v in s_a["power"]) or any(v is not None for v in s_b["power"]):
                fig.add_trace(go.Scatter(x=s_a["x_min"], y=s_a["power"], mode="lines", name="Race A Power", line=dict(color="#dc2626", width=2)))
                fig.add_trace(go.Scatter(x=s_b["x_min"], y=s_b["power"], mode="lines", name="Race B Power", line=dict(color="#2563eb", width=2)))
                fig.update_yaxes(title_text="Power (W)")
            else:
                notes = "No power channel found; showing HR if available."

            # Hidden speed traces for client-side avg speed computation
            speed_a_kmh = [(v * 3.6 if v is not None and v > 0 else None) for v in s_a["speed"]]
            speed_b_kmh = [(v * 3.6 if v is not None and v > 0 else None) for v in s_b["speed"]]
            fig.add_trace(go.Scatter(x=s_a["x_min"], y=speed_a_kmh, mode="lines", name="Race A Speed", visible=False))
            fig.add_trace(go.Scatter(x=s_b["x_min"], y=speed_b_kmh, mode="lines", name="Race B Speed", visible=False))

            if any(v is not None for v in s_a["hr"]) or any(v is not None for v in s_b["hr"]):
                fig.add_trace(go.Scatter(x=s_a["x_min"], y=s_a["hr"], mode="lines", name="Race A HR", line=dict(color="#dc2626", width=1, dash="dot"), yaxis="y2"))
                fig.add_trace(go.Scatter(x=s_b["x_min"], y=s_b["hr"], mode="lines", name="Race B HR", line=dict(color="#2563eb", width=1, dash="dot"), yaxis="y2"))
                fig.update_layout(yaxis2=dict(title="Heart Rate (bpm)", overlaying="y", side="right"))
            if fig.layout.yaxis.title is None:
                fig.update_yaxes(title_text="Value")

        else:
            title = "Swim: Pace"
            y_a = [_pace_min_per_100m(v) for v in s_a["speed"]]
            y_b = [_pace_min_per_100m(v) for v in s_b["speed"]]
            fig.add_trace(go.Scatter(x=s_a["x_min"], y=y_a, mode="lines", name="Race A Pace", line=dict(color="#dc2626", width=2)))
            fig.add_trace(go.Scatter(x=s_b["x_min"], y=y_b, mode="lines", name="Race B Pace", line=dict(color="#2563eb", width=2)))
            fig.update_yaxes(title_text="Pace (min/100m)")

        fig.update_xaxes(title_text="Minutes")
        fig.update_layout(
            height=420,
            margin=dict(l=50, r=50, t=30, b=45),
            legend=dict(orientation="h"),
        )

        # Combine cache info with any sport-specific notes
        all_notes = " ".join([n for n in (cache_note_parts[0] if cache_note_parts else None, notes) if n])

        fig_json = fig.to_json() if fig.data else None

        has_power_a = any(v is not None for v in s_a["power"])
        has_power_b = any(v is not None for v in s_b["power"])

        # Build athlete short name for filenames (e.g. "JSmith")
        athlete_short = ""
        if athlete and athlete.name:
            parts = athlete.name.strip().split()
            if len(parts) >= 2:
                athlete_short = parts[0][0] + parts[-1]
            elif parts:
                athlete_short = parts[0]

        # Build race labels for download filenames (e.g. "2025-06-15 Hamburg WTCS")
        race_label_a = ""
        race_label_b = ""
        key_a = _parse_event_prog_key(compare_race_a)
        key_b = _parse_event_prog_key(compare_race_b)
        if key_a or key_b:
            with get_session() as session:
                if key_a:
                    r = session.execute(
                        select(WTORaceResult.event_date, WTORaceResult.event_name)
                        .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                        .where(WTORaceResult.event_id == int(key_a[0]))
                        .where(WTORaceResult.prog_id == int(key_a[1]))
                        .limit(1)
                    ).first()
                    if r:
                        race_label_a = f"{r.event_date} {r.event_name}"
                if key_b:
                    r = session.execute(
                        select(WTORaceResult.event_date, WTORaceResult.event_name)
                        .where(WTORaceResult.podium_athlete_id == int(target_athlete_id))
                        .where(WTORaceResult.event_id == int(key_b[0]))
                        .where(WTORaceResult.prog_id == int(key_b[1]))
                        .limit(1)
                    ).first()
                    if r:
                        race_label_b = f"{r.event_date} {r.event_name}"

        return templates.TemplateResponse(
            "partials/race_tp_traces.html",
            {
                "request": request,
                "error": None,
                "fig_json": fig_json,
                "title": title,
                "notes": all_notes or None,
                "target_athlete_id": target_athlete_id,
                "sport": sport,
                "tp_workout_a": wid_a,
                "tp_workout_b": wid_b,
                "has_power_a": has_power_a,
                "has_power_b": has_power_b,
                "race_label_a": race_label_a,
                "race_label_b": race_label_b,
                "athlete_short": athlete_short,
            },
        )

    @app.post("/actions/sync_race_results", response_class=HTMLResponse)
    def action_sync_race_results(
        request: Request,
        target_athlete_id: int = Form(...),
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, int(target_athlete_id)):
            raise HTTPException(status_code=403, detail="Forbidden")

        try:
            summary = sync_race_results_last_two_years(int(target_athlete_id))
        except Exception as e:
            return HTMLResponse(f"<div class='muted'>Race sync failed: {e}</div>")

        years = sorted(
            {
                int(r["event_date"].year)
                for r in load_local_race_results(int(target_athlete_id))
                if isinstance(r.get("event_date"), date)
            },
            reverse=True,
        )
        years_text = ", ".join(str(y) for y in years) if years else "none"

        inserted = int(summary.get("inserted") or 0)
        range_txt = str(summary.get("range") or "window unknown")
        return HTMLResponse(
            f"<div class='muted'>"
            f"Race sync complete: inserted {inserted} rows ({range_txt}). Years in cache: {years_text}."
            "</div>"
            "<script>"
            "(function () {"
            "  if (window.htmx) {"
            "    htmx.trigger(document.body, 'podiumRefresh');"
            "  }"
            "})();"
            "</script>"
        )

    # ── Manual FIT file upload ────────────────────────────────────────────────
    @app.post("/actions/upload_fit", response_class=HTMLResponse)
    async def action_upload_fit(
        request: Request,
        file: UploadFile = File(...),
        target_athlete_id: int = Form(...),
        sport: str = Form("run"),
        label: str = Form(""),
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        filename = file.filename or ""
        if not filename.lower().endswith(".fit"):
            return HTMLResponse("<div class='muted'>Only .fit files are supported.</div>")

        content = await file.read()
        if not content:
            return HTMLResponse("<div class='muted'>Uploaded file is empty.</div>")

        # Parse FIT bytes → timeseries
        try:
            payload = parse_fit_to_timeseries(content)
        except Exception as e:
            return HTMLResponse(f"<div class='muted'>Could not parse FIT file: {e}</div>")

        channels = (payload.get("WorkoutChannels") or {}).get("Channels") or []
        data_rows = (payload.get("WorkoutChannels") or {}).get("Data") or []
        if not data_rows:
            return HTMLResponse("<div class='muted'>FIT file parsed but contained no time-series records.</div>")

        # Extract date and sport from FIT session if available
        import io, hashlib
        try:
            import fitparse
            fit = fitparse.FitFile(io.BytesIO(content))
            sessions = list(fit.get_messages("session"))
            if sessions:
                start_ts = sessions[0].get_value("start_time")
                fit_date = start_ts.date() if start_ts and hasattr(start_ts, "date") else None
                fit_sport_raw = sessions[0].get_value("sport") or ""
                # normalize fitparse sport string (e.g. "cycling" → "bike")
                fs = str(fit_sport_raw).lower()
                if fs in ("cycling", "bike", "biking"):
                    fit_sport = "bike"
                elif fs in ("swimming", "swim"):
                    fit_sport = "swim"
                elif fs in ("running", "run"):
                    fit_sport = "run"
                else:
                    fit_sport = sport  # fall back to form value
                if fit_date:
                    workout_date = fit_date
                else:
                    workout_date = date.today()
                sport = fit_sport
            else:
                workout_date = date.today()
        except Exception:
            workout_date = date.today()

        # Stable ID based on content hash
        digest = hashlib.sha256(content).hexdigest()[:14]
        workout_id = f"manual_{digest}"

        # Save to timeseries cache
        save_timeseries(workout_id, payload)

        # Upsert a Workout row so it appears in the compare dropdown
        duration_sec: int | None = None
        if data_rows:
            last_ms = data_rows[-1].get("MillisecondOffset") or 0
            try:
                duration_sec = int(float(last_ms) / 1000)
            except Exception:
                duration_sec = None

        with get_session() as session:
            existing = session.execute(
                select(Workout).where(Workout.tp_workout_id == workout_id).limit(1)
            ).scalars().first()
            if existing:
                existing.date = workout_date
                existing.sport = sport
                existing.duration_sec = duration_sec
                existing.raw_json = {"label": label or filename, "channels": channels}
            else:
                session.add(Workout(
                    athlete_id=int(target_athlete_id),
                    tp_workout_id=workout_id,
                    date=workout_date,
                    sport=sport,
                    duration_sec=duration_sec,
                    raw_json={"label": label or filename, "channels": channels},
                ))
            session.commit()

        dur_str = _format_duration(duration_sec) if duration_sec else "?"
        display_label = label or filename
        return HTMLResponse(
            f"<div class='muted' style='color:#16a34a;'>"
            f"✓ Uploaded: {display_label} — {workout_date} • {sport} • {dur_str} • {len(data_rows):,} points"
            f"</div>"
            f"<div class='muted' style='margin-top:4px;'>This workout will now appear in the Race Day comparison dropdowns for races around {workout_date}.</div>"
        )

    # ── Timeseries CSV download ──────────────────────────────────────────────
    @app.get("/api/timeseries_csv")
    def api_timeseries_csv(
        request: Request,
        target_athlete_id: int,
        tp_workout_id: str,
        sport: str = "run",
        race_label: str = "",
        athlete_short: str = "",
        requester_id: int = Depends(require_login),
    ):
        """Download full-resolution time series as CSV.

        Bike: elapsed_seconds, speed_m_s, power_watts, heart_rate_bpm, cadence_rpm, elevation_m
        Run:  elapsed_seconds, speed_m_s, heart_rate_bpm, cadence_spm, elevation_m
        """
        import csv
        import io

        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        wid = tp_workout_id.strip()
        if not wid:
            raise HTTPException(status_code=400, detail="Missing tp_workout_id")

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))
        if not athlete or not athlete.tp_athlete_id:
            raise HTTPException(status_code=404, detail="Athlete not found or missing tp_athlete_id")

        api = get_api(int(target_athlete_id))
        tp_aid = int(athlete.tp_athlete_id)

        try:
            payload = fetch_timeseries_cached(api, wid, tp_aid)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not load timeseries: {e}")

        channels, rows = _extract_channels_payload(payload)
        if not rows:
            raise HTTPException(status_code=404, detail="No timeseries data in this workout")

        idx_time = _find_channel_index(channels, {"millisecondoffset", "time", "seconds", "sec", "elapsedtime", "elapsedseconds"})
        idx_speed = _find_channel_index(channels, {"speed", "velocity", "vel"})
        idx_power = _find_channel_index(channels, {"power", "watts", "w"})
        idx_hr = _find_channel_index(channels, {"heartrate", "hr"})
        idx_cadence = _find_channel_index(channels, {"cadence"})
        idx_elevation = _find_channel_index(channels, {"elevation", "altitude", "alt"})

        time_is_ms = False
        if idx_time is not None:
            ch_name = (channels[idx_time] or "").strip().lower().replace(" ", "")
            time_is_ms = "millisecond" in ch_name

        sport_norm = _sport_norm(sport) or "run"

        buf = io.StringIO()
        writer = csv.writer(buf)

        if sport_norm == "bike":
            writer.writerow(["elapsed_seconds", "speed_m_s", "power_watts", "heart_rate_bpm", "cadence_rpm", "elevation_m"])
        else:
            writer.writerow(["elapsed_seconds", "speed_m_s", "heart_rate_bpm", "cadence_spm", "elevation_m"])

        def _val(row, idx):
            if idx is None or idx >= len(row):
                return ""
            v = row[idx]
            if v is None:
                return ""
            return v

        for r in rows:
            if idx_time is not None and idx_time < len(r):
                try:
                    raw_t = float(r[idx_time])
                    t_sec = raw_t / 1000.0 if time_is_ms else raw_t
                except Exception:
                    t_sec = ""
            else:
                t_sec = ""

            speed_val = _val(r, idx_speed)
            hr_val = _val(r, idx_hr)
            cadence_val = _val(r, idx_cadence)
            elev_val = _val(r, idx_elevation)
            if sport_norm == "bike":
                power_val = _val(r, idx_power)
                writer.writerow([t_sec, speed_val, power_val, hr_val, cadence_val, elev_val])
            else:
                writer.writerow([t_sec, speed_val, hr_val, cadence_val, elev_val])

        buf.seek(0)

        # Build a descriptive filename from race label + athlete or fall back to workout ID
        import re as _re
        safe_athlete = _re.sub(r'[^\w]', '', athlete_short).strip() if athlete_short else ""
        if race_label:
            safe_label = _re.sub(r'[^\w\s-]', '', race_label).strip().replace(' ', '_')
            parts = [safe_athlete, safe_label, sport_norm] if safe_athlete else [safe_label, sport_norm]
            filename = "_".join(parts) + ".csv"
        else:
            parts = [safe_athlete, "timeseries", sport_norm, wid] if safe_athlete else ["timeseries", sport_norm, wid]
            filename = "_".join(parts) + ".csv"
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/timeseries_xlsx")
    def api_timeseries_xlsx(
        request: Request,
        target_athlete_id: int,
        tp_workout_id: str,
        sport: str = "run",
        race_label: str = "",
        athlete_short: str = "",
        trimmed: bool = False,
        t_start: float | None = None,
        t_end: float | None = None,
        requester_id: int = Depends(require_login),
    ):
        """Download time series as Excel matching the lab-software template.

        Row 1: time | value | (empty C–AR)
        Row 2: (s) | (m/s or watt) | 32 fixed lab variable names (C–AH) |
               sensor data labels replacing Var1–Var10 (AI–AR)
        Data:  time | power-or-speed | empty C–AH | sensor values in AI+

        Optional filters (applied before trimming):
        - t_start / t_end: keep only rows within this time window (seconds).
          Typically set from the chart zoom range.
        - trimmed=true: additionally strip leading zeros and trailing zeros,
          skipping past large time gaps (>60 s).
        Time is always reset to start at 0 when any filter is active.
        """
        import io
        from openpyxl import Workbook
        from openpyxl.styles import Font

        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        wid = tp_workout_id.strip()
        if not wid:
            raise HTTPException(status_code=400, detail="Missing tp_workout_id")

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))
        if not athlete or not athlete.tp_athlete_id:
            raise HTTPException(status_code=404, detail="Athlete not found or missing tp_athlete_id")

        api = get_api(int(target_athlete_id))
        tp_aid = int(athlete.tp_athlete_id)

        try:
            payload = fetch_timeseries_cached(api, wid, tp_aid)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not load timeseries: {e}")

        channels, rows = _extract_channels_payload(payload)
        if not rows:
            raise HTTPException(status_code=404, detail="No timeseries data in this workout")

        idx_time = _find_channel_index(channels, {"millisecondoffset", "time", "seconds", "sec", "elapsedtime", "elapsedseconds"})
        idx_speed = _find_channel_index(channels, {"speed", "velocity", "vel"})
        idx_power = _find_channel_index(channels, {"power", "watts", "w"})
        idx_hr = _find_channel_index(channels, {"heartrate", "hr"})
        idx_cadence = _find_channel_index(channels, {"cadence"})
        idx_elevation = _find_channel_index(channels, {"elevation", "altitude", "alt"})

        time_is_ms = False
        if idx_time is not None:
            ch_name = (channels[idx_time] or "").strip().lower().replace(" ", "")
            time_is_ms = "millisecond" in ch_name

        sport_norm = _sport_norm(sport) or "run"
        is_bike = sport_norm == "bike"

        # 32 fixed lab variable names for columns C (3) through AH (34)
        LAB_VARS = [
            "LaB_meas", "VO2_meas", "Fat-g/h_meas", "CHO-g/h_meas",
            "PCr_meas", "ATP_meas", "Pi_meas", "pHB_meas", "pHM_meas",
            "Hb-Ox_meas", "%OX_meas", "SO2%_meas",
            "LaB_static", "VO2_static", "phM_static", "Fat-g/h_static",
            "CHO-g/h_static", "VLa_static", "%aerob_static", "%anaerobic_static",
            "cdA", "Crr", "Torso_angle", "knee_angle",
            "pressure", "altitude", "yaw", "effect_wind",
            "Torque", "Force", "CHO_intake", "Glycogen",
        ]  # 32 items → cols C–AH

        # Sensor data labels replace Var1_meas … Var10_meas (cols AI–AR)
        REMAINING_VARS = 10  # Var1 through Var10 slot
        if is_bike:
            sensor_labels = ["Speed", "HeartRate", "Cadence", "Elevation"]
        else:
            sensor_labels = ["HeartRate", "Cadence", "Elevation"]
        var_labels = sensor_labels + [
            f"Var{i}_meas" for i in range(len(sensor_labels) + 1, REMAINING_VARS + 1)
        ]

        wb = Workbook()
        ws = wb.active
        ws.title = "Data"

        # Row 1: only time and value have text; rest empty through col AR
        row1 = ["time", "value"] + [None] * len(LAB_VARS) + [None] * REMAINING_VARS
        ws.append(row1)

        # Row 2: units + all variable names (red font)
        row2 = ["(s)", "(m/s or watt)"] + LAB_VARS + var_labels
        ws.append(row2)
        red_font = Font(color="FF0000")
        for cell in ws[2]:
            cell.font = red_font

        # Helper
        def _num(row, idx):
            if idx is None or idx >= len(row):
                return None
            v = row[idx]
            if v is None:
                return None
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        # Build parsed rows (for optional trimming)
        parsed = []
        for r in rows:
            t_sec = None
            if idx_time is not None and idx_time < len(r):
                try:
                    raw_t = float(r[idx_time])
                    t_sec = raw_t / 1000.0 if time_is_ms else raw_t
                except Exception:
                    pass

            speed = _num(r, idx_speed) or 0.0
            power = _num(r, idx_power) or 0.0
            value = power if is_bike else speed

            if is_bike:
                sensors = [_num(r, idx_speed), _num(r, idx_hr), _num(r, idx_cadence), _num(r, idx_elevation)]
            else:
                sensors = [_num(r, idx_hr), _num(r, idx_cadence), _num(r, idx_elevation)]

            parsed.append((t_sec, value, speed, power, sensors))

        # ── Time-window filter (from chart zoom) ────────────────────
        if t_start is not None or t_end is not None:
            lo = t_start if t_start is not None else float('-inf')
            hi = t_end if t_end is not None else float('inf')
            parsed = [(t, val, spd, pwr, sn) for (t, val, spd, pwr, sn) in parsed
                      if t is not None and lo <= t <= hi]

        if trimmed and parsed:
            # Trim leading dead time: skip until speed > 0
            start = 0
            for i, (t, val, spd, pwr, sn) in enumerate(parsed):
                if spd > 0:
                    start = i
                    break

            # If there's a large time gap (>60 s) after initial movement,
            # the real race starts after the last big gap.
            for i in range(start + 1, len(parsed)):
                prev_t = parsed[i - 1][0]
                cur_t = parsed[i][0]
                if prev_t is not None and cur_t is not None and (cur_t - prev_t) > 60:
                    start = i

            # Trim trailing dead time (scan from end)
            end = len(parsed) - 1
            if is_bike:
                for i in range(len(parsed) - 1, start - 1, -1):
                    if parsed[i][2] > 0 or parsed[i][3] > 0:
                        end = i
                        break
            else:
                for i in range(len(parsed) - 1, start - 1, -1):
                    if parsed[i][2] > 0:
                        end = i
                        break

            parsed = parsed[start:end + 1]

        # Reset time to start at 0 whenever any filtering was applied
        if (trimmed or t_start is not None or t_end is not None) and parsed:
            if parsed[0][0] is not None:
                t_offset = parsed[0][0]
                parsed = [(((t - t_offset) if t is not None else None), val, spd, pwr, sn)
                          for (t, val, spd, pwr, sn) in parsed]

        # Write data rows: A=time, B=value, C–AH empty, AI+ sensors
        empty_lab = [None] * len(LAB_VARS)
        empty_remaining = [None] * (REMAINING_VARS - len(sensor_labels))
        for (t_sec, value, _spd, _pwr, sensors) in parsed:
            ws.append([t_sec, value] + empty_lab + sensors + empty_remaining)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        import re as _re
        safe_athlete = _re.sub(r'[^\w]', '', athlete_short).strip() if athlete_short else ""
        trim_tag = "_raceonly" if trimmed else ("_zoom" if (t_start is not None or t_end is not None) else "")
        if race_label:
            safe_label = _re.sub(r'[^\w\s-]', '', race_label).strip().replace(' ', '_')
            parts = [safe_athlete, safe_label, sport_norm] if safe_athlete else [safe_label, sport_norm]
            filename = "_".join(parts) + trim_tag + ".xlsx"
        else:
            parts = [safe_athlete, "timeseries", sport_norm, wid] if safe_athlete else ["timeseries", sport_norm, wid]
            filename = "_".join(parts) + trim_tag + ".xlsx"

        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── Workout Explorer (any TP workout, not tied to triathlon races) ───────
    @app.get("/partials/workout_explorer", response_class=HTMLResponse)
    def partial_workout_explorer(
        request: Request,
        target_athlete_id: int,
        explorer_date: str | None = None,
        explorer_sport: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        day = _parse_date(explorer_date)
        if not day:
            return templates.TemplateResponse(
                "partials/workout_explorer.html",
                {"request": request, "error": None, "workouts": None, "explorer_date": ""},
            )

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))
        if not athlete or not athlete.tp_athlete_id:
            return templates.TemplateResponse(
                "partials/workout_explorer.html",
                {"request": request, "error": "Athlete missing TrainingPeaks ID.", "workouts": None, "explorer_date": str(day)},
            )

        selected_sport = _sport_norm(explorer_sport) or ""
        api = get_api(int(target_athlete_id))
        start = day - timedelta(days=1)
        end = day + timedelta(days=1)
        try:
            raw_workouts = api.fetch_workouts(start, end, tp_athlete_id=int(athlete.tp_athlete_id))
        except Exception as e:
            return templates.TemplateResponse(
                "partials/workout_explorer.html",
                {"request": request, "error": f"Error fetching workouts: {e}", "workouts": None, "explorer_date": str(day)},
            )

        workouts: list[dict] = []
        for w in raw_workouts or []:
            wid = w.get("workoutId") or w.get("WorkoutId") or w.get("id") or w.get("Id")
            if not wid:
                continue
            sport = w.get("WorkoutType") or w.get("sportType") or w.get("sport") or ""
            title = w.get("Title") or w.get("title") or w.get("WorkoutName") or ""
            date_field = w.get("workoutDay") or w.get("WorkoutDay") or w.get("Date") or w.get("date")
            w_day = _coerce_date(date_field)
            completed = bool(w.get("Completed", False))
            dur_sec = None
            if completed and w.get("TotalTime"):
                try:
                    val = float(w.get("TotalTime"))
                    dur_sec = int(val * 3600) if val < 20 else int(val)
                except Exception:
                    dur_sec = None

            tss_val = w.get("TssActual") if completed else None
            if_val = w.get("IF") if completed else None

            parts = []
            if w_day:
                parts.append(str(w_day))
            if sport:
                parts.append(str(sport))
            if title:
                parts.append(str(title))
            meta = []
            if dur_sec:
                meta.append(_format_duration(int(dur_sec)))
            if tss_val is not None:
                try:
                    meta.append(f"TSS {float(tss_val):.0f}")
                except Exception:
                    pass
            if if_val is not None:
                try:
                    meta.append(f"IF {float(if_val):.2f}")
                except Exception:
                    pass

            label = " • ".join([p for p in parts if p])
            if meta:
                label = f"{label} — {', '.join(meta)}"
            workouts.append({"workout_id": str(wid), "sport": str(sport or ""), "duration_sec": dur_sec, "label": label, "title": title, "date": str(w_day or day)})

        workouts.sort(
            key=lambda x: (
                0 if selected_sport and _sport_matches(x.get("sport"), selected_sport) else 1,
                -(int(x.get("duration_sec") or 0)),
                x.get("label") or "",
            )
        )

        sport_options = [
            {"value": "", "label": "All"},
            {"value": "run", "label": "Run"},
            {"value": "bike", "label": "Bike"},
            {"value": "swim", "label": "Swim"},
        ]

        return templates.TemplateResponse(
            "partials/workout_explorer.html",
            {
                "request": request,
                "error": None,
                "workouts": workouts,
                "sport_options": sport_options,
                "selected_sport": selected_sport,
                "explorer_date": str(day),
                "target_athlete_id": target_athlete_id,
            },
        )

    @app.get("/partials/workout_trace", response_class=HTMLResponse)
    def partial_workout_trace(
        request: Request,
        target_athlete_id: int,
        explorer_workout: str | None = None,
        explorer_trace_sport: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        """Load a single-workout trace chart with CSV download."""
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        wid = (explorer_workout or "").strip()
        sport = _sport_norm(explorer_trace_sport) or "run"
        if not wid:
            return templates.TemplateResponse(
                "partials/workout_trace.html",
                {"request": request, "error": "Select a workout first.", "fig_json": None, "title": "", "notes": None},
            )

        with get_session() as session:
            athlete = session.get(Athlete, int(target_athlete_id))
        if not athlete or not athlete.tp_athlete_id:
            return templates.TemplateResponse(
                "partials/workout_trace.html",
                {"request": request, "error": "Missing tp_athlete_id.", "fig_json": None, "title": "", "notes": None},
            )

        api = get_api(int(target_athlete_id))
        tp_aid = int(athlete.tp_athlete_id)
        notes = None

        try:
            cached = is_timeseries_cached(wid)
            payload = fetch_timeseries_cached(api, wid, tp_aid)
            cache_note = "Loaded from cache." if cached else "Fetched from TP and cached."
        except Exception as e:
            return templates.TemplateResponse(
                "partials/workout_trace.html",
                {"request": request, "error": str(e), "fig_json": None, "title": "", "notes": None},
            )

        channels, rows = _extract_channels_payload(payload)
        if not rows:
            return templates.TemplateResponse(
                "partials/workout_trace.html",
                {"request": request, "error": "No time-series data in this workout.", "fig_json": None, "title": "", "notes": None},
            )

        idx_time = _find_channel_index(channels, {"millisecondoffset", "time", "seconds", "sec", "elapsedtime", "elapsedseconds"})
        idx_speed = _find_channel_index(channels, {"speed", "velocity", "vel"})
        idx_hr = _find_channel_index(channels, {"heartrate", "hr"})
        idx_power = _find_channel_index(channels, {"power", "watts", "w"})

        time_is_ms = False
        if idx_time is not None:
            ch_name = (channels[idx_time] or "").strip().lower().replace(" ", "")
            time_is_ms = "millisecond" in ch_name

        n = len(rows)
        max_points = 1400
        stride = max(1, int(math.ceil(n / max_points)))

        xs: list[float] = []
        speed: list[float | None] = []
        hr: list[float | None] = []
        power: list[float | None] = []

        for i in range(0, n, stride):
            r = rows[i]
            if idx_time is not None and idx_time < len(r):
                try:
                    raw_t = float(r[idx_time])
                    t_sec = raw_t / 1000.0 if time_is_ms else raw_t
                except Exception:
                    t_sec = float(i)
            else:
                t_sec = float(i)
            xs.append(t_sec / 60.0)

            def _get(idx: int | None) -> float | None:
                if idx is None or idx >= len(r):
                    return None
                try:
                    v = r[idx]
                    return float(v) if v is not None else None
                except Exception:
                    return None

            speed.append(_get(idx_speed))
            hr.append(_get(idx_hr))
            power.append(_get(idx_power))

        import plotly.graph_objects as go

        def _pace_min_per_mile(speed_mps: float | None) -> float | None:
            if speed_mps is None or speed_mps <= 0:
                return None
            return (1609.34 / speed_mps) / 60.0

        fig = go.Figure()
        title = ""

        if sport == "run":
            title = "Run: Pace + Heart Rate"
            y_pace = [_pace_min_per_mile(v) for v in speed]
            fig.add_trace(go.Scatter(x=xs, y=y_pace, mode="lines", name="Pace", line=dict(color="#dc2626", width=2)))
            if any(v is not None for v in hr):
                fig.add_trace(go.Scatter(x=xs, y=hr, mode="lines", name="HR", line=dict(color="#2563eb", width=1, dash="dot"), yaxis="y2"))
                fig.update_layout(yaxis2=dict(title="Heart Rate (bpm)", overlaying="y", side="right"))
            fig.update_yaxes(title_text="Pace (min/mi)")
        elif sport == "bike":
            title = "Bike: Power + Heart Rate"
            if any(v is not None for v in power):
                fig.add_trace(go.Scatter(x=xs, y=power, mode="lines", name="Power", line=dict(color="#dc2626", width=2)))
                fig.update_yaxes(title_text="Power (W)")
            else:
                notes = "No power channel found; showing HR if available."
            if any(v is not None for v in hr):
                fig.add_trace(go.Scatter(x=xs, y=hr, mode="lines", name="HR", line=dict(color="#2563eb", width=1, dash="dot"), yaxis="y2"))
                fig.update_layout(yaxis2=dict(title="Heart Rate (bpm)", overlaying="y", side="right"))
        else:
            title = "Swim: Pace"
            def _pace_100m(s):
                return (100.0 / s) / 60.0 if s and s > 0 else None
            y_pace = [_pace_100m(v) for v in speed]
            fig.add_trace(go.Scatter(x=xs, y=y_pace, mode="lines", name="Pace", line=dict(color="#dc2626", width=2)))
            fig.update_yaxes(title_text="Pace (min/100m)")

        fig.update_xaxes(title_text="Minutes")
        fig.update_layout(height=420, margin=dict(l=50, r=50, t=30, b=45), legend=dict(orientation="h"))

        all_notes = " ".join(n for n in (cache_note, notes) if n)
        fig_json = fig.to_json() if fig.data else None
        has_power = any(v is not None for v in power)

        # Build athlete short name for filename
        athlete_short = ""
        if athlete and athlete.name:
            name_parts = athlete.name.strip().split()
            if len(name_parts) >= 2:
                athlete_short = name_parts[0][0] + name_parts[-1]
            elif name_parts:
                athlete_short = name_parts[0]

        return templates.TemplateResponse(
            "partials/workout_trace.html",
            {
                "request": request,
                "error": None,
                "fig_json": fig_json,
                "title": title,
                "notes": all_notes or None,
                "target_athlete_id": target_athlete_id,
                "sport": sport,
                "explorer_workout": wid,
                "has_power": has_power,
                "athlete_short": athlete_short,
            },
        )

    @app.get("/partials/coach_tab", response_class=HTMLResponse)
    def partial_coach_tab(request: Request, tab: str, _: int = Depends(require_coach)):
        tab_norm = (tab or "").strip().lower()
        if tab_norm in {"overview", "roster", "roster_overview"}:
            template = "partials/coach_tab_overview.html"
        elif tab_norm in {"compliance", "daily_compliance", "workout_compliance"}:
            template = "partials/coach_tab_compliance.html"
        elif tab_norm in {"recovery", "metrics", "daily_metrics", "recovery_metrics"}:
            template = "partials/coach_tab_recovery.html"
        elif tab_norm in {"race", "races", "performance", "race_performance"}:
            template = "partials/coach_tab_race.html"
        elif tab_norm in {"prediction", "predictions", "predict"}:
            template = "partials/coach_tab_prediction.html"
        else:
            raise HTTPException(status_code=400, detail="Invalid tab")

        today = get_effective_today()
        ctx = {
            "request": request,
            "default_overview_date": today,
            "default_compliance_date": today,
            "default_end": today,
            "default_start": today - timedelta(days=14),
        }
        return templates.TemplateResponse(template, ctx)

    @app.get("/partials/coach_overview", response_class=HTMLResponse)
    def partial_coach_overview(
        request: Request,
        day: str | None = None,
        coach_id: int = Depends(require_coach),
    ):
        day_d = _parse_date(day) or get_effective_today()

        with get_session() as session:
            roster_stmt = (
                select(Athlete)
                .join(CoachRosterMember, CoachRosterMember.athlete_id == Athlete.id)
                .where(CoachRosterMember.coach_athlete_id == int(coach_id))
                .order_by(Athlete.name)
            )
            roster = session.execute(roster_stmt).scalars().all()
            athlete_ids = [int(a.id) for a in (roster or [])]

            comp_by_athlete: dict[int, list[WorkoutCompliance]] = defaultdict(list)
            if athlete_ids:
                comp_stmt = (
                    select(WorkoutCompliance)
                    .where(WorkoutCompliance.athlete_id.in_(athlete_ids))
                    .where(WorkoutCompliance.workout_date == day_d)
                )
                for wc in session.execute(comp_stmt).scalars().all():
                    comp_by_athlete[int(wc.athlete_id)].append(wc)

            metric_by_athlete: dict[int, list[MetricAlert]] = defaultdict(list)
            if athlete_ids:
                alert_stmt = (
                    select(MetricAlert)
                    .where(MetricAlert.athlete_id.in_(athlete_ids))
                    .where(MetricAlert.alert_date == day_d)
                    .where(MetricAlert.severity.in_(["yellow", "red"]))
                )
                for al in session.execute(alert_stmt).scalars().all():
                    metric_by_athlete[int(al.athlete_id)].append(al)

            fatigue_triggered: set[int] = set()
            if athlete_ids:
                run_stmt = (
                    select(RecoveryAlertRun)
                    .where(RecoveryAlertRun.athlete_id.in_(athlete_ids))
                    .where(RecoveryAlertRun.alert_date == day_d)
                    .where(RecoveryAlertRun.triggered.is_(True))
                )
                for run in session.execute(run_stmt).scalars().all():
                    fatigue_triggered.add(int(run.athlete_id))

        def _completed_flag(wc: WorkoutCompliance) -> bool:
            actual = getattr(wc, "actual_summary", None)
            if isinstance(actual, dict):
                return bool(actual.get("completed"))
            return False

        def _bucket(wc: WorkoutCompliance) -> str:
            # Buckets match the daily compliance view: missed (not completed), then good/ok/bad by score.
            if not _completed_flag(wc):
                return "missed"
            score = getattr(wc, "overall_score", None)
            if score is None:
                return "ok"
            try:
                score_f = float(score)
            except Exception:
                return "ok"
            if score_f >= 85:
                return "good"
            if score_f >= 70:
                return "ok"
            return "bad"

        rows = []
        totals = {
            "roster_size": len(roster or []),
            "athletes_with_red_workout": 0,
            "athletes_with_missed_workout": 0,
            "athletes_with_fatigue_flag": 0,
            "athletes_with_metric_alert": 0,
        }

        for a in (roster or []):
            aid = int(a.id)
            compliances = comp_by_athlete.get(aid, [])

            workouts_total = len(compliances)
            workouts_missed = 0
            workouts_red = 0

            for wc in compliances:
                b = _bucket(wc)
                if b == "missed":
                    workouts_missed += 1
                elif b == "bad":
                    workouts_red += 1

            alerts = metric_by_athlete.get(aid, [])
            metric_red = sum(1 for al in alerts if (getattr(al, "severity", "") or "").lower() == "red")
            metric_yellow = sum(1 for al in alerts if (getattr(al, "severity", "") or "").lower() == "yellow")

            fatigue = aid in fatigue_triggered

            if workouts_red > 0:
                totals["athletes_with_red_workout"] += 1
            if workouts_missed > 0:
                totals["athletes_with_missed_workout"] += 1
            if fatigue:
                totals["athletes_with_fatigue_flag"] += 1
            if (metric_red + metric_yellow) > 0:
                totals["athletes_with_metric_alert"] += 1

            status = "blue"
            if workouts_total > 0:
                status = "green"
            if metric_yellow > 0:
                status = "yellow"
            if workouts_red > 0 or workouts_missed > 0 or fatigue or metric_red > 0:
                status = "red"

            rows.append(
                {
                    "athlete_id": aid,
                    "athlete_name": getattr(a, "name", f"Athlete {aid}"),
                    "status": status,
                    "workouts_total": workouts_total,
                    "workouts_missed": workouts_missed,
                    "workouts_red": workouts_red,
                    "fatigue_triggered": fatigue,
                    "metric_red": metric_red,
                    "metric_yellow": metric_yellow,
                }
            )

        # Sort: red first, then yellow, then green/blue; within group sort by name.
        severity_rank = {"red": 0, "yellow": 1, "green": 2, "blue": 3}
        rows.sort(key=lambda r: (severity_rank.get(r.get("status"), 9), str(r.get("athlete_name") or "")))

        return templates.TemplateResponse(
            "partials/coach_overview.html",
            {
                "request": request,
                "day": day_d.isoformat(),
                "totals": totals,
                "rows": rows,
            },
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

    @app.get("/partials/alerts_overview", response_class=HTMLResponse)
    def partial_alerts_overview(
        request: Request,
        target_athlete_id: int,
        start: str,
        end: str,
        recovery_triggered_only: str | None = None,
        recovery_include_ok: str | None = None,
        metric_name: str | None = None,
        metric_severity: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        start_d = _parse_date(start)
        end_d = _parse_date(end)
        if not start_d or not end_d:
            raise HTTPException(status_code=400, detail="Invalid date range")
        if end_d < start_d:
            raise HTTPException(status_code=400, detail="End date must be >= start date")
        days_total = (end_d - start_d).days + 1
        if days_total > 365:
            raise HTTPException(status_code=400, detail="Range too large (max 365 days)")

        triggered_only = _is_truthy(recovery_triggered_only)
        include_ok = _is_truthy(recovery_include_ok)

        metric_name_norm = (metric_name or "").strip()
        metric_sev_norm = (metric_severity or "").strip().lower()
        if metric_sev_norm and metric_sev_norm not in {"green", "yellow", "red"}:
            metric_sev_norm = ""

        # Recovery runs
        runs_all = list_recovery_alert_runs(int(target_athlete_id), start=start_d, end=end_d, limit=500)
        runs = []
        for r in (runs_all or []):
            if triggered_only and not getattr(r, "triggered", False):
                continue
            if not include_ok and not getattr(r, "triggered", False):
                continue
            runs.append(r)

        # Metric alerts
        with get_session() as session:
            stmt = (
                select(MetricAlert)
                .where(MetricAlert.athlete_id == int(target_athlete_id))
                .where(MetricAlert.alert_date >= start_d)
                .where(MetricAlert.alert_date <= end_d)
            )
            if metric_name_norm:
                stmt = stmt.where(MetricAlert.metric_name == metric_name_norm)
            if metric_sev_norm:
                stmt = stmt.where(MetricAlert.severity == metric_sev_norm)
            stmt = stmt.order_by(MetricAlert.alert_date.desc()).limit(1500)
            alerts = session.execute(stmt).scalars().all()

        triggered_days = {getattr(r, "alert_date", None) for r in (runs_all or []) if getattr(r, "triggered", False)}
        triggered_days = {d for d in triggered_days if isinstance(d, date)}
        summary = {
            "days_total": days_total,
            "metric_alerts": len(alerts or []),
            "recovery_triggered_days": len(triggered_days),
            "recovery_runs": len(runs_all or []),
        }
        return templates.TemplateResponse(
            "partials/alerts_overview.html",
            {
                "request": request,
                "summary": summary,
                "runs": runs,
                "alerts": alerts,
            },
        )

    @app.get("/partials/recovery_alert_runs", response_class=HTMLResponse)
    def partial_recovery_alert_runs(
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

        runs = list_recovery_alert_runs(int(target_athlete_id), start=start_d, end=end_d, limit=500)
        return templates.TemplateResponse(
            "partials/recovery_alert_runs.html",
            {"request": request, "runs": runs},
        )

    @app.post("/partials/backfill_alerts", response_class=HTMLResponse)
    def partial_backfill_alerts(
        request: Request,
        target_athlete_id: int | None = Form(None),
        start: str | None = Form(None),
        end: str | None = Form(None),
        requester_id: int = Depends(require_coach),
    ):
        # Coach-only action; uses stored DB data only (no TP calls).
        if target_athlete_id is None or not start or not end:
            return templates.TemplateResponse(
                "partials/alerts_backfill.html",
                {
                    "request": request,
                    "error": "Backfill request is missing fields (target_athlete_id/start/end). Try refreshing the page and ensure Start/End dates are set.",
                    "summary": {
                        "days_total": 0,
                        "days_with_metrics": 0,
                        "metric_alerts_created": 0,
                        "recovery_triggered": 0,
                    },
                },
            )

        role = request.session.get("role") or "coach"
        if not can_access_athlete(requester_id, role, int(target_athlete_id)):
            raise HTTPException(status_code=403, detail="Forbidden")

        start_d = _parse_date(start)
        end_d = _parse_date(end)
        if not start_d or not end_d:
            raise HTTPException(status_code=400, detail="Invalid date range")
        if end_d < start_d:
            raise HTTPException(status_code=400, detail="End date must be >= start date")

        days_total = (end_d - start_d).days + 1
        if days_total > 365:
            raise HTTPException(status_code=400, detail="Range too large (max 365 days)")

        metric_alerts_created = 0
        recovery_triggered = 0
        days_with_metrics = 0
        recovery_threshold = 0.05

        with get_session() as session:
            metric_dates = session.execute(
                select(DailyMetric.date)
                .where(DailyMetric.athlete_id == int(target_athlete_id))
                .where(DailyMetric.date >= start_d)
                .where(DailyMetric.date <= end_d)
                .order_by(DailyMetric.date.asc())
            ).scalars().all()

        metric_dates = sorted({d for d in metric_dates if isinstance(d, date)})
        days_with_metrics = len(metric_dates)

        if days_with_metrics == 0:
            return templates.TemplateResponse(
                "partials/alerts_backfill.html",
                {
                    "request": request,
                    "error": "No daily metrics found in the database for this athlete/date range. If this athlete is non-premium in TrainingPeaks, daily metrics may not be ingested.",
                    "summary": {
                        "days_total": days_total,
                        "days_with_metrics": 0,
                        "metric_alerts_created": 0,
                        "recovery_triggered": 0,
                    },
                },
            )

        try:
            for day in metric_dates:
                # Baseline snapshots for this day
                calculate_baselines(int(target_athlete_id), end_date=day)

                # Metric alerts (persisted)
                created = check_alert_conditions(int(target_athlete_id), check_date=day)
                metric_alerts_created += len(created or [])

                # Recovery alert evaluation (persisted)
                recovery = evaluate_recovery_alert(
                    int(target_athlete_id),
                    check_date=day,
                    threshold=recovery_threshold,
                    send_email=False,
                )
                if isinstance(recovery, dict):
                    if recovery.get("triggered"):
                        recovery_triggered += 1
                    upsert_recovery_alert_run(
                        int(target_athlete_id),
                        day,
                        threshold=recovery_threshold,
                        triggered=bool(recovery.get("triggered")),
                        reason=str(recovery.get("reason") or ""),
                        metrics=(recovery.get("metrics") or {}),
                    )

            resp = templates.TemplateResponse(
                "partials/alerts_backfill.html",
                {
                    "request": request,
                    "error": None,
                    "summary": {
                        "days_total": days_total,
                        "days_with_metrics": days_with_metrics,
                        "metric_alerts_created": metric_alerts_created,
                        "recovery_triggered": recovery_triggered,
                    },
                },
            )
            resp.headers["HX-Trigger"] = "podiumRefresh"
            return resp
        except Exception as e:
            return templates.TemplateResponse(
                "partials/alerts_backfill.html",
                {
                    "request": request,
                    "error": f"Backfill failed: {e}",
                    "summary": {
                        "days_total": days_total,
                        "days_with_metrics": days_with_metrics,
                        "metric_alerts_created": metric_alerts_created,
                        "recovery_triggered": recovery_triggered,
                    },
                },
            )

    @app.get("/partials/compliance_today", response_class=HTMLResponse)
    def partial_compliance_today(
        request: Request,
        target_athlete_id: int,
        compliance_date: str | None = None,
        requester_id: int = Depends(require_login),
    ):
        role = request.session.get("role") or "athlete"
        if not can_access_athlete(requester_id, role, target_athlete_id):
            raise HTTPException(status_code=403, detail="Forbidden")

        day = _parse_date(compliance_date) or get_effective_today()
        snapshot = compliance_service.get_compliance_for_day(int(target_athlete_id), day) or {}
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
                "today": day,
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

    @app.post("/jobs/sync_roster_recent")
    def sync_roster_recent_job(
        request: Request,
        days: int = Form(7),
        coach_id: int = Depends(require_coach),
    ):
        job = enqueue_job(
            "sync_roster_recent",
            requested_by_athlete_id=int(coach_id),
            target_athlete_id=None,
            payload={"days": int(days)},
        )
        return templates.TemplateResponse(
            "partials/job_enqueued.html",
            {"request": request, "job_id": int(job.id)},
        )

    # ── Prediction Tab ──

    @app.get("/partials/prediction_events", response_class=HTMLResponse)
    def partial_prediction_events(request: Request, _: int = Depends(require_coach)):
        """Return dropdown options for upcoming events with start lists."""
        from app.services.prediction import fetch_upcoming_events

        tri_engine = get_triathlon_engine()
        if tri_engine is None:
            return "<div class='muted'>Triathlon database not configured.</div>"
        events = fetch_upcoming_events(tri_engine)
        return templates.TemplateResponse(
            "partials/prediction_events.html",
            {"request": request, "events": events},
        )

    @app.get("/partials/prediction_programs", response_class=HTMLResponse)
    def partial_prediction_programs(
        request: Request,
        event_id: int,
        _: int = Depends(require_coach),
    ):
        """Return program options for a selected event."""
        from app.services.prediction import fetch_event_programs

        tri_engine = get_triathlon_engine()
        if tri_engine is None:
            return "<div class='muted'>Triathlon database not configured.</div>"
        programs = fetch_event_programs(tri_engine, event_id)
        return templates.TemplateResponse(
            "partials/prediction_programs.html",
            {"request": request, "programs": programs},
        )

    @app.get("/partials/prediction_start_list", response_class=HTMLResponse)
    def partial_prediction_start_list(
        request: Request,
        event_id: int,
        prog_id: int,
        _: int = Depends(require_coach),
    ):
        """Load the start list for a selected event + program."""
        from app.services.prediction import fetch_start_list_for_program

        athletes = fetch_start_list_for_program(event_id, prog_id)
        return templates.TemplateResponse(
            "partials/prediction_start_list.html",
            {"request": request, "athletes": athletes, "event_id": event_id, "prog_id": prog_id},
        )

    @app.get("/partials/prediction_athlete_search", response_class=HTMLResponse)
    def partial_prediction_athlete_search(
        request: Request,
        q: str = "",
        exclude: str = "",
        _: int = Depends(require_coach),
    ):
        """Debounced athlete name search for adding to start list."""
        from app.services.prediction import search_athletes_in_triathlon_db

        exclude_ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()] if exclude else None
        results = search_athletes_in_triathlon_db(q, exclude_ids=exclude_ids)
        return templates.TemplateResponse(
            "partials/prediction_search_results.html",
            {"request": request, "results": results},
        )

    @app.post("/partials/prediction_simulate", response_class=HTMLResponse)
    async def partial_prediction_simulate(
        request: Request,
        event_id: int = Form(...),
        prog_id: int = Form(...),
        breakaway_bias: float = Form(0.0),
        form_share: float = Form(0.2),
        _: int = Depends(require_coach),
    ):
        """Run full prediction pipeline and return results."""
        from app.services.prediction import run_prediction_pipeline

        # Collect athlete_ids from repeated form fields
        form_data = await request.form()
        athlete_ids_raw = form_data.getlist("athlete_ids")
        athlete_ids = [int(x) for x in athlete_ids_raw if str(x).strip().isdigit()] or None

        result = run_prediction_pipeline(
            event_id=event_id,
            prog_id=prog_id,
            breakaway_bias=breakaway_bias,
            form_share=form_share,
            athlete_ids=athlete_ids,
        )
        return templates.TemplateResponse(
            "partials/prediction_results.html",
            {
                "request": request,
                "result": result,
                "event_id": event_id,
                "prog_id": prog_id,
            },
        )

    @app.post("/partials/prediction_resimulate", response_class=HTMLResponse)
    async def partial_prediction_resimulate(
        request: Request,
        event_id: int = Form(...),
        prog_id: int = Form(...),
        breakaway_bias: float = Form(0.0),
        form_share: float = Form(0.2),
        _: int = Depends(require_coach),
    ):
        """Re-simulate with adjusted parameters (uses cached features)."""
        from app.services.prediction import resimulate

        form_data = await request.form()
        athlete_ids_raw = form_data.getlist("athlete_ids")
        athlete_ids = [int(x) for x in athlete_ids_raw if str(x).strip().isdigit()] or None

        result = resimulate(
            event_id=event_id,
            prog_id=prog_id,
            breakaway_bias=breakaway_bias,
            form_share=form_share,
            athlete_ids=athlete_ids,
        )
        return templates.TemplateResponse(
            "partials/prediction_results.html",
            {
                "request": request,
                "result": result,
                "event_id": event_id,
                "prog_id": prog_id,
            },
        )

    # ---- Public Rankings Dashboard ----

    RANKING_CATEGORIES = {
        13: "World Rankings — Men",
        14: "World Rankings — Women",
        15: "WTCS Rankings — Men",
        16: "WTCS Rankings — Women",
    }
    WTCS_CATEGORIES = {15, 16}

    @app.get("/rankings")
    def rankings_page(request: Request):
        tri_engine = get_triathlon_engine()
        countries = []
        if tri_engine:
            with tri_engine.connect() as conn:
                try:
                    with conn.begin_nested():
                        rows = conn.execute(text("""
                            SELECT DISTINCT a.country
                            FROM athlete a
                            JOIN athlete_rankings ar ON a.athlete_id = ar.athlete_id
                            WHERE a.country IS NOT NULL AND a.country != ''
                            ORDER BY a.country
                        """)).fetchall()
                        countries = [r[0] for r in rows]
                except Exception:
                    countries = []
        return templates.TemplateResponse("rankings.html", {
            "request": request,
            "categories": RANKING_CATEGORIES,
            "countries": countries,
            "default_cat_id": 13,
        })

    @app.get("/partials/rankings_table", response_class=HTMLResponse)
    def partial_rankings_table(
        request: Request,
        cat_id: int = 13,
        country: str = "",
        search: str = "",
        page: int = 1,
        per_page: int = 50,
    ):
        tri_engine = get_triathlon_engine()
        if not tri_engine:
            return HTMLResponse("<p>Triathlon database not configured.</p>")

        with tri_engine.connect() as conn:
            # Full leaderboard for rank-drop simulation (unfiltered)
            full_lb = conn.execute(text("""
                WITH cd AS (
                    SELECT MAX(retrieved_at) AS latest FROM athlete_rankings
                    WHERE ranking_cat_id = :cat_id
                )
                SELECT ar.athlete_id, ar.total_points
                FROM athlete_rankings ar JOIN cd ON ar.retrieved_at = cd.latest
                WHERE ar.ranking_cat_id = :cat_id
                ORDER BY ar.total_points DESC
            """), {"cat_id": cat_id}).fetchall()
            all_pts = [float(r[1] or 0) for r in full_lb]

            # Filtered + paginated query
            filters = ["ar.ranking_cat_id = :cat_id"]
            params: dict = {"cat_id": cat_id}
            if country:
                filters.append("a.country = :country")
                params["country"] = country
            if search:
                filters.append("ar.athlete_name ILIKE :search")
                params["search"] = f"%{search}%"

            where = " AND ".join(filters)

            # Count total
            count_sql = f"""
                WITH cd AS (
                    SELECT MAX(retrieved_at) AS latest FROM athlete_rankings
                    WHERE ranking_cat_id = :cat_id
                )
                SELECT COUNT(*)
                FROM athlete_rankings ar
                JOIN cd ON ar.retrieved_at = cd.latest
                JOIN athlete a ON ar.athlete_id = a.athlete_id
                WHERE {where}
            """
            total_count = conn.execute(text(count_sql), params).scalar()

            # Paginated rows
            offset = (page - 1) * per_page
            params["limit"] = per_page
            params["offset"] = offset
            data_sql = f"""
                WITH cd AS (
                    SELECT MAX(retrieved_at) AS latest FROM athlete_rankings
                    WHERE ranking_cat_id = :cat_id
                )
                SELECT ar.athlete_id, ar.athlete_name, ar.rank_position,
                       ar.total_points, ar.events_current_period,
                       ar.events_previous_period, a.country
                FROM athlete_rankings ar
                JOIN cd ON ar.retrieved_at = cd.latest
                JOIN athlete a ON ar.athlete_id = a.athlete_id
                WHERE {where}
                ORDER BY ar.rank_position
                LIMIT :limit OFFSET :offset
            """
            rows = [dict(r) for r in conn.execute(text(data_sql), params).mappings().all()]

            # At-risk computation for this page's athletes
            page_athlete_ids = [r["athlete_id"] for r in rows]
            at_risk: dict[int, dict] = {}
            event_counts: dict[int, dict] = {}  # {athlete_id: {curr: n, prev: n}}
            if page_athlete_ids:
                try:
                    with conn.begin_nested():
                        bd_rows = conn.execute(text("""
                            WITH latest_per_athlete AS (
                                SELECT athlete_id, MAX(retrieved_at) AS latest
                                FROM athlete_ranking_breakdown
                                WHERE athlete_id = ANY(:ids)
                                  AND ranking_cat_id = :cat_id
                                GROUP BY athlete_id
                            )
                            SELECT abd.athlete_id, abd.event_id, abd.event_finish_date,
                                   abd.points, abd.period, abd.included
                            FROM athlete_ranking_breakdown abd
                            JOIN latest_per_athlete lpa
                              ON abd.athlete_id = lpa.athlete_id
                             AND abd.retrieved_at = lpa.latest
                            WHERE abd.ranking_cat_id = :cat_id
                        """), {"ids": page_athlete_ids, "cat_id": cat_id}).mappings().all()
                except Exception:
                    bd_rows = []

                today = date.today()
                cutoff_2w = today + timedelta(days=14)
                cutoff_1m = today + timedelta(days=30)
                cutoff_3m = today + timedelta(days=91)
                athlete_events: dict[int, list[dict]] = {}
                seen_events: set[tuple[int, int]] = set()

                def _bucket_and_value_at(ev: dict, as_of: date) -> tuple[int | None, float]:
                    pts = float(ev.get("points") or 0.0)
                    efd = ev.get("event_finish_date")
                    period = int(ev.get("period", 1) or 1)

                    if pts <= 0:
                        return None, 0.0

                    if not efd:
                        return (2, pts) if period == 2 else (1, pts)

                    one_year = efd + timedelta(days=365)
                    two_years = efd + timedelta(days=730)
                    if as_of >= two_years:
                        return None, 0.0

                    if as_of < one_year:
                        return 1, pts

                    # In period 2 (12-24 months), events currently in period 1 lose 2/3 value.
                    # Events already in period 2 are assumed to already be period-adjusted in DB.
                    if period == 1:
                        return 2, (pts / 3.0)
                    return 2, pts

                def _score_at(events: list[dict], as_of: date, cap_curr: int, cap_prev: int) -> float:
                    curr_values: list[float] = []
                    prev_values: list[float] = []

                    for ev in events:
                        bucket, val = _bucket_and_value_at(ev, as_of)
                        if bucket is None or val <= 0:
                            continue
                        if bucket == 1:
                            curr_values.append(val)
                        else:
                            prev_values.append(val)

                    if not curr_values and not prev_values:
                        return 0.0

                    curr_values.sort(reverse=True)
                    prev_values.sort(reverse=True)

                    curr_cap = cap_curr if cap_curr > 0 else len(curr_values)
                    prev_cap = cap_prev if cap_prev > 0 else len(prev_values)

                    return sum(curr_values[:curr_cap]) + sum(prev_values[:prev_cap])

                for bd in bd_rows:
                    aid = bd["athlete_id"]
                    event_id = bd.get("event_id")
                    if event_id is not None:
                        dedupe_key = (aid, int(event_id))
                        if dedupe_key in seen_events:
                            continue
                        seen_events.add(dedupe_key)
                    is_included = bd.get("included", True)
                    period = bd.get("period", 1)

                    # Count included events per period
                    if is_included:
                        if aid not in event_counts:
                            event_counts[aid] = {"curr": 0, "prev": 0}
                        if period == 1:
                            event_counts[aid]["curr"] += 1
                        else:
                            event_counts[aid]["prev"] += 1

                    athlete_events.setdefault(aid, []).append({
                        "event_finish_date": bd.get("event_finish_date"),
                        "points": float(bd.get("points") or 0.0),
                        "period": period,
                        "included": is_included,
                    })

                # At-risk simulation:
                # 1) events crossing 12 months drop to one-third points
                # 2) expiring events can be replaced by currently excluded events
                for aid, events in athlete_events.items():
                    caps = event_counts.get(aid, {})
                    cap_curr = int(caps.get("curr", 0) or 0)
                    cap_prev = int(caps.get("prev", 0) or 0)

                    current_score = _score_at(events, today, cap_curr, cap_prev)

                    risk_2w = max(0.0, current_score - _score_at(events, cutoff_2w, cap_curr, cap_prev))
                    risk_1m = max(0.0, current_score - _score_at(events, cutoff_1m, cap_curr, cap_prev))
                    risk_3m = max(0.0, current_score - _score_at(events, cutoff_3m, cap_curr, cap_prev))

                    at_risk[aid] = {"r2w": risk_2w, "r1m": risk_1m, "r3m": risk_3m}

                # Backfill curr/prev event counts into rows where NULL
                for r in rows:
                    aid = r["athlete_id"]
                    if r.get("events_current_period") is None and aid in event_counts:
                        r["events_current_period"] = event_counts[aid]["curr"]
                    if r.get("events_previous_period") is None and aid in event_counts:
                        r["events_previous_period"] = event_counts[aid]["prev"]

            # Rank-drop simulation
            drops: dict[int, dict] = {}
            for r in rows:
                aid = r["athlete_id"]
                cur_pts = float(r["total_points"] or 0)
                cur_rank = sum(1 for p in all_pts if p > cur_pts) + 1
                risk = at_risk.get(aid, {})
                d = {}
                for suffix in ("2w", "1m", "3m"):
                    lost = risk.get(f"r{suffix}", 0)
                    if lost:
                        new_pts = cur_pts - lost
                        new_rank = sum(1 for p in all_pts if p > new_pts) + 1
                        d[suffix] = new_rank - cur_rank
                    else:
                        d[suffix] = 0
                drops[aid] = d

            # Weekly change: compare current rank to ~one week ago.
            # Pick the most recent snapshot at least 6 days before the latest
            # so stray midweek snapshots don't collapse "change" to ~0.
            changes: dict[int, int | None] = {}
            try:
                prev_week = conn.execute(text("""
                    SELECT DISTINCT retrieved_at FROM athlete_rankings
                    WHERE ranking_cat_id = :cat_id
                      AND retrieved_at <= (
                          SELECT MAX(retrieved_at) FROM athlete_rankings
                          WHERE ranking_cat_id = :cat_id
                      ) - INTERVAL '6 days'
                    ORDER BY retrieved_at DESC LIMIT 1
                """), {"cat_id": cat_id}).scalar()
            except Exception:
                prev_week = None

            if prev_week:
                prev_rows = conn.execute(text("""
                    SELECT athlete_id, rank_position
                    FROM athlete_rankings
                    WHERE ranking_cat_id = :cat_id AND retrieved_at = :prev_date
                """), {"cat_id": cat_id, "prev_date": prev_week}).fetchall()
                prev_ranks = {r[0]: r[1] for r in prev_rows}
                for r in rows:
                    aid = r["athlete_id"]
                    old_rank = prev_ranks.get(aid)
                    if old_rank is not None:
                        changes[aid] = old_rank - r["rank_position"]  # positive = moved up
                    else:
                        changes[aid] = None  # new entry

        total_pages = max(1, math.ceil(total_count / per_page))
        is_wtcs = cat_id in WTCS_CATEGORIES
        return templates.TemplateResponse("partials/rankings_table.html", {
            "request": request,
            "athletes": rows,
            "at_risk": at_risk,
            "drops": drops,
            "changes": changes,
            "event_counts": event_counts,
            "is_wtcs": is_wtcs,
            "cat_id": cat_id,
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "country": country,
            "search": search,
        })

    @app.get("/partials/rankings_breakdown/{cat_id}/{athlete_id}", response_class=HTMLResponse)
    def partial_rankings_breakdown(request: Request, cat_id: int, athlete_id: int):
        tri_engine = get_triathlon_engine()
        if not tri_engine:
            return HTMLResponse("<p>Triathlon database not configured.</p>")

        with tri_engine.connect() as conn:
            try:
                with conn.begin_nested():
                    rows = conn.execute(text("""
                        SELECT event_id, event_title, event_finish_date, points,
                               period, position, included
                        FROM (
                            SELECT DISTINCT ON (event_id)
                                event_id, event_title, event_finish_date, points,
                                period, position, included, retrieved_at
                            FROM athlete_ranking_breakdown
                            WHERE athlete_id = :aid AND ranking_cat_id = :cat_id
                            ORDER BY event_id, retrieved_at DESC
                        ) latest
                        ORDER BY period, points DESC
                    """), {"aid": athlete_id, "cat_id": cat_id}).mappings().all()
            except Exception:
                rows = []

            name_row = conn.execute(text("""
                SELECT athlete_name FROM athlete_rankings
                WHERE athlete_id = :aid AND ranking_cat_id = :cat_id
                ORDER BY retrieved_at DESC LIMIT 1
            """), {"aid": athlete_id, "cat_id": cat_id}).fetchone()

        today = date.today()
        period1 = []
        period2 = []
        for r in rows:
            entry = dict(r)
            if r["event_finish_date"]:
                entry["drop_off"] = r["event_finish_date"] + timedelta(days=365)
                entry["days_to_drop_off"] = (entry["drop_off"] - today).days
                entry["drop_off_loss"] = float(r.get("points") or 0.0) * (2.0 / 3.0)
                entry["expiry"] = r["event_finish_date"] + timedelta(days=730)
                entry["days_to_expiry"] = (entry["expiry"] - today).days
            else:
                entry["drop_off"] = None
                entry["days_to_drop_off"] = None
                entry["drop_off_loss"] = None
                entry["expiry"] = None
                entry["days_to_expiry"] = None
            if r["period"] == 1:
                period1.append(entry)
            else:
                period2.append(entry)

        is_wtcs = cat_id in WTCS_CATEGORIES
        return templates.TemplateResponse("partials/rankings_athlete_detail.html", {
            "request": request,
            "athlete_name": name_row[0] if name_row else f"Athlete {athlete_id}",
            "athlete_id": athlete_id,
            "cat_id": cat_id,
            "period1": period1,
            "period2": period2,
            "is_wtcs": is_wtcs,
        })

    @app.get("/partials/rankings_rank_trend/{cat_id}/{athlete_id}", response_class=HTMLResponse)
    def partial_rankings_rank_trend(request: Request, cat_id: int, athlete_id: int):
        import plotly.graph_objects as go
        import plotly.utils

        tri_engine = get_triathlon_engine()
        if not tri_engine:
            return HTMLResponse("<p>Triathlon database not configured.</p>")

        try:
            with tri_engine.connect() as conn:
                computed_rows = conn.execute(text("""
                    SELECT ranking_date, rank_position
                    FROM computed_weekly_rankings
                    WHERE athlete_id = :aid AND ranking_cat_id = :cat_id
                    ORDER BY ranking_date
                """), {"aid": athlete_id, "cat_id": cat_id}).fetchall()

                # Last two real snapshots from World Triathlon — always shown as
                # the chart's tail so the trend line ends at the current rank.
                real_rows = conn.execute(text("""
                    SELECT retrieved_at, rank_position
                    FROM athlete_rankings
                    WHERE athlete_id = :aid AND ranking_cat_id = :cat_id
                    ORDER BY retrieved_at DESC
                    LIMIT 2
                """), {"aid": athlete_id, "cat_id": cat_id}).fetchall()
        except Exception as e:
            return HTMLResponse(
                f'<p class="muted" style="text-align:center; font-size:13px; padding:12px 0;">'
                f'Chart unavailable: {e}</p>'
            )

        # Tail: (oldest_real_date, rank), (newest_real_date, rank) in chronological order
        tail = sorted(((r[0], r[1]) for r in real_rows), key=lambda x: x[0])

        # Drop any computed rows on/after the earliest real snapshot — real data
        # supersedes the simulated series from that point forward.
        if tail:
            cutoff = tail[0][0]
            merged = [(r[0], r[1]) for r in computed_rows if r[0] < cutoff] + tail
        else:
            merged = [(r[0], r[1]) for r in computed_rows]

        if not merged:
            return HTMLResponse(
                '<p class="muted" style="text-align:center; font-size:13px; padding:12px 0;">'
                'No historical ranking data available for this athlete.</p>'
            )

        try:
            dates = [d.isoformat() for d, _ in merged]
            ranks = [r for _, r in merged]

            # Default visible range: last 12 months
            from datetime import date, timedelta
            range_end = date.today()
            range_start = range_end - timedelta(days=365)

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=dates,
                y=ranks,
                mode="lines+markers",
                marker=dict(size=4, color="#2563eb"),
                line=dict(color="#2563eb", width=2),
                hovertemplate="<b>%{x}</b><br>Rank: %{y}<extra></extra>",
            ))

            fig.update_layout(
                margin=dict(l=40, r=20, t=10, b=40),
                height=220,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(
                    # Range is set dynamically in JS based on visible x-range
                    title="Rank",
                    title_font=dict(size=11),
                    tickfont=dict(size=10),
                    gridcolor="#e5e7eb",
                ),
                xaxis=dict(
                    type="date",
                    range=[range_start.isoformat(), range_end.isoformat()],
                    rangeslider=dict(visible=True, thickness=0.08),
                    rangeselector=dict(
                        buttons=[
                            dict(count=6, label="6M", step="month", stepmode="backward"),
                            dict(count=1, label="1Y", step="year", stepmode="backward"),
                            dict(count=2, label="2Y", step="year", stepmode="backward"),
                            dict(step="all", label="All"),
                        ],
                        font=dict(size=11),
                        bgcolor="#f1f5f9",
                        activecolor="#2563eb",
                    ),
                    tickfont=dict(size=10),
                    gridcolor="#e5e7eb",
                ),
                font=dict(family="inherit"),
                showlegend=False,
            )

            chart_json = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
        except Exception as e:
            return HTMLResponse(
                f'<p class="muted" style="text-align:center; font-size:13px; padding:12px 0;">'
                f'Chart unavailable: {e}</p>'
            )

        div_id = f"rank-trend-{athlete_id}"
        html = f"""
<div style="margin-top:16px;">
  <h5 style="margin:0 0 8px; font-size:13px; color:#374151;">Rank Over Time</h5>
  <div id="{div_id}"></div>
</div>
<script>
  (function() {{
    var fig = {chart_json};
    var divId = '{div_id}';
    Plotly.react(divId, fig.data, fig.layout, {{responsive: true, displayModeBar: false}}).then(function() {{
      var el = document.getElementById(divId);
      var xs = fig.data[0].x.map(function(d) {{ return new Date(d).getTime(); }});
      var ys = fig.data[0].y;

      function fitY(xStart, xEnd) {{
        var t0 = new Date(xStart).getTime();
        var t1 = new Date(xEnd).getTime();
        var visible = [];
        for (var i = 0; i < xs.length; i++) {{
          if (xs[i] >= t0 && xs[i] <= t1) visible.push(ys[i]);
        }}
        if (!visible.length) visible = ys;
        var lo = Math.min.apply(null, visible);
        var hi = Math.max.apply(null, visible);
        var pad = Math.max(1, Math.ceil((hi - lo) * 0.1));
        // reversed axis: worse rank (bigger) at bottom, best (1) at top
        Plotly.relayout(divId, {{'yaxis.range': [hi + pad, Math.max(0, lo - pad)]}});
      }}

      // Seed initial y-range from the default 1Y window
      var initRange = (fig.layout.xaxis && fig.layout.xaxis.range) || null;
      if (initRange) fitY(initRange[0], initRange[1]);

      el.on('plotly_relayout', function(ed) {{
        if (ed['xaxis.range[0]'] !== undefined && ed['xaxis.range[1]'] !== undefined) {{
          fitY(ed['xaxis.range[0]'], ed['xaxis.range[1]']);
        }} else if (ed['xaxis.range'] && ed['xaxis.range'].length === 2) {{
          fitY(ed['xaxis.range'][0], ed['xaxis.range'][1]);
        }} else if (ed['xaxis.autorange']) {{
          fitY(xs[0], xs[xs.length - 1]);
        }}
      }});
    }});
  }})();
</script>
"""
        return HTMLResponse(html)

    register_compare_routes(app, templates)

    return app


app = create_app()
