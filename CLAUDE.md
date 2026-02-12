# PodiumDashboard

## Overview
Coach/athlete dashboard for USA Triathlon. Ingests training data from TrainingPeaks, computes baselines, compliance, recovery alerts, and visualizes athlete performance. Recently migrated from Streamlit to **FastAPI + HTMX**.

## Tech Stack
- **Backend**: FastAPI + Uvicorn (port 8000)
- **Frontend**: Jinja2 templates + HTMX partial fragments
- **Database**: PostgreSQL (Supabase) via SQLAlchemy 2.0
- **Auth**: OAuth 2.0 against TrainingPeaks (sandbox)
- **Background Jobs**: APScheduler + Postgres-backed job queue (no Redis)
- **Worker**: `podium_worker.py` — polls job queue, runs syncs
- **Python**: 3.12+, virtual env at `.venv/`

## Running Locally
```powershell
# FastAPI web server
uvicorn app.webapp.app:app --reload --port 8000

# Background worker (separate terminal)
python podium_worker.py
```

## Project Structure
```
app/
├── auth/           # OAuth flow (TrainingPeaks)
├── data/           # DB sessions (db.py, triathlon_db.py)
├── models/         # SQLAlchemy ORM tables
├── services/       # Business logic (compliance, ingest, tp_api, llm, etc.)
├── scheduling/     # APScheduler daily jobs
├── ui/             # Legacy Streamlit views
├── utils/          # Settings (Pydantic), date helpers
└── webapp/         # FastAPI app + Jinja2/HTMX templates
    ├── app.py      # Main FastAPI application and routes
    └── templates/  # base.html, coach.html, me.html, partials/
```

## Key Conventions
- HTMX partials live in `app/webapp/templates/partials/` — each returns an HTML fragment, not a full page
- Routes return `TemplateResponse` for full pages, raw HTML strings for HTMX partials
- Coach role sees roster athletes; Athlete role sees only self
- Session auth via signed cookies (SECRET_KEY in .env)
- All DB access goes through SQLAlchemy sessions from `app/data/db.py`
- Tests use pytest: `pytest tests/ -q`

## Environment Variables
Defined in `.env` (see `.env.example`). Key vars:
- `DATABASE_URL` — Supabase Postgres connection string
- `TRIATHLON_DATABASE_URL` — Read-only access to triathlon-db's database (separate instance)
- `TP_CLIENT_ID`, `TP_CLIENT_SECRET` — TrainingPeaks OAuth
- `SECRET_KEY` — Session cookie signing
- `HEAD_COACH_EMAIL` — Coach account identifier

## Cross-Repo Integration: triathlon-db
The `triathlon-db` repo (sibling at `../../triathlon-db/`) contains:
- **ML prediction models** (`tri_analysis/prediction/`) — trained gradient boosting models for race time prediction
- **Monte Carlo simulation** (`tri_analysis/prediction/simulate.py`) — probabilistic race outcome simulation with pack dynamics
- **Feature engineering** (`tri_analysis/prediction/features.py`) — EWMA form, event tiers, pack metrics
- **Trained model bundles** (`models/bundle_*.joblib`)
- **Separate database** with World Triathlon race results and rankings

Integration approach is TBD — may use editable install (`pip install -e`) for direct imports, subprocess calls, or both.

## Shell
Use PowerShell for all terminal commands on this Windows machine.
