# Podium Dashboard

MVP Streamlit application to ingest TrainingPeaks data, compute recovery / risk signals, and present a coaching dashboard.

## Features (Planned)
- OAuth to TrainingPeaks Sandbox
- Backfill athlete historical workouts & daily metrics
- Daily 07:30 America/Denver sync & risk assessment
- Rolling baselines, ACWR, HRV/RHR/sleep composite
- Email summary to head coach

## Tech Stack
Python, Streamlit, Authlib, SQLAlchemy, Postgres (Supabase), APScheduler, SendGrid.

## Quick Start
1. Copy `.env.example` to `.env` and fill in secrets (TrainingPeaks client id/secret + DATABASE_URL).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the app:
   ```bash
   streamlit run app/main.py
   ```
4. Open "Connect TrainingPeaks" page and click Start OAuth Flow; authorize in Sandbox.
5. Redirect URI now uses root path `http://localhost:8501/` so the app can process `?code=` directly (Streamlit cannot serve `/oauth/callback`).
6. Tables are auto-created on first run via `Base.metadata.create_all()` (no Alembic yet).

## Data Model (Initial Draft)
Tables (to be created via Alembic):
- athletes(id, external_id, name, email, created_at)
- oauth_tokens(id, athlete_id, access_token, refresh_token, expires_at, scope, provider)
- workouts(id, athlete_id, tp_workout_id, date, sport, duration_sec, tss, intensity_factor, raw_json, created_at)
- daily_metrics(id, athlete_id, date, rhr, hrv, sleep_hours, body_score, ctl, atl, tsb, created_at)
- aggregates(id, athlete_id, date, acute_load, chronic_load, acwr, hrv_baseline, rhr_baseline, sleep_baseline, created_at)
- risk_assessments(id, athlete_id, date, risk_level, reasons, created_at)
- email_log(id, athlete_id, date, email_type, status, created_at)

## Development Notes
- Single hard-coded athlete bootstrap in code until UI add flow exists.
- All times stored in UTC in DB; convert to America/Denver in UI.
- Scheduler uses APScheduler background thread inside Streamlit (single process assumption).
- Schema changes require manual table adjustments now (later: introduce Alembic).

## Next Steps
- Implement OAuth flow & token storage.
- Build backfill service & baseline calculations.
- Implement daily scheduler job.
- Add risk scoring logic.
- Add email sending (fallback to log if no API key).

## Manual Sync & Data Display
- Use the Dashboard page slider to select days (default 7) and click "Manual Sync" to pull recent workouts + today's daily metric.
- Scheduler runs daily at configured time calling the same ingestion routine.
- UI displays recent workouts (date, sport, duration, TSS, IF) and latest daily metrics (RHR, HRV, Sleep, CTL, ATL, TSB).

## OAuth Scopes & Roles
The Connect page now provides a role selector:
- Athlete role scopes: athlete:profile metrics:read workouts:read workouts:details
- Coach role scopes: coach:athletes metrics:read workouts:read workouts:details

TrainingPeaks Sandbox currently rejects combining athlete + coach scopes in one request; select the appropriate role then start the flow. Re-authorization uses the same selected role.

## Endpoint Assumptions
Current implementation assumes:
- Workouts endpoint: `GET /v3/workouts?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD`
- Daily metrics endpoint: `GET /v3/metrics/{YYYY-MM-DD}`
Adjust `app/services/tp_api.py` if official endpoint paths differ.

## License
Internal MVP (license TBD).
