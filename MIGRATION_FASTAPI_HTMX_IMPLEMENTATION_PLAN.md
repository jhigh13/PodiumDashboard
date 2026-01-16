# FastAPI + HTMX Migration Plan (Podium Dashboard)

Date: 2026-01-15

## Goal
Replace the Streamlit UI with a more professional web app that supports:
- TrainingPeaks-only login for **athletes** and **coaches**
- Athletes can only view themselves
- Coaches can only view athletes on their TrainingPeaks roster
- Fast, button-driven UI updates (partial refresh) without Streamlit reruns
- Background syncing (scheduled + on-demand) so UI stays responsive

The existing Streamlit app remains intact during migration to keep a reliable fallback.

## Proposed Target Architecture
### Processes
1) **Web**: FastAPI server
- Serves pages (HTML) and HTMX partial endpoints
- Handles OAuth login/callback
- Enqueues background jobs (sync roster, sync training)

2) **Worker**: Python worker process
- Polls Postgres for queued jobs and executes them
- Runs APScheduler for daily sync; scheduler enqueues jobs (does not do heavy work inline)

3) **DB**: Supabase Postgres
- Source of truth for athletes, workouts, metrics, baselines, alerts, compliance
- New tables: `jobs`, `coach_roster_members` (DB-backed job queue + coach->athlete permissions)

## Design Decisions (initial)
- **Identity model**: reuse existing `athletes` table as the login identity (no separate `users` table initially)
  - On OAuth callback, we fetch TrainingPeaks profile to get a stable `tp_athlete_id`.
  - We upsert an `athletes` row with `external_id = tp_{tp_athlete_id}`.
  - We store the OAuth token in the existing `oauth_tokens` table keyed by that athlete row.
- **Role model**: role is determined by token scopes (coach scope implies coach).
- **Roster permissions**: a new mapping table `coach_roster_members` restricts coach access.
- **Queue**: DB-backed queue using Postgres row locks (`FOR UPDATE SKIP LOCKED`). No Redis/Celery required initially.

## Implementation Phases
### Phase 0 — Safety + Baseline
- Keep Streamlit working.
- Add a new branch for migration work.

### Phase 1 — Job Queue + Worker
- Add models/tables:
  - `jobs` (type/status/payload/result/error timestamps)
  - `coach_roster_members` (coach_athlete_id -> athlete_id)
- Add worker process:
  - Loop: claim one queued job, mark running, execute, mark success/failure.
  - Add scheduled daily job that enqueues sync jobs.

### Phase 2 — FastAPI Web App Skeleton
- Add FastAPI app entrypoint.
- Add session middleware (cookie session using `SECRET_KEY`).
- Add OAuth routes:
  - `/login` (role selection)
  - `/oauth/callback` (token exchange + profile fetch + token store)
  - On coach login: automatically enqueue a roster sync job.
- Add minimal pages:
  - `/me` athlete dashboard shell
  - `/coach` coach dashboard shell (athlete selector limited to roster)

### Phase 3 — HTMX Partials (Fast UI)
- Implement partial endpoints:
  - metrics/workouts/alerts/compliance panels
- Build the dashboard with partial refresh:
  - date range controls trigger only panel refreshes

### Phase 4 — “Sync Now” UX
- Add button-driven sync:
  - `/jobs/sync_recent` creates job
  - UI polls `/partials/job_status` and refreshes panels when complete

### Phase 5 — Hardening
- Job retry + stale job detection
- Basic access logs / audit trail
- Introduce Alembic migrations once schema stabilizes

## Checkpoints (success criteria)
1) **Worker runs** and can claim a queued job from Postgres.
2) **Coach login works** (TP OAuth) and creates/updates the coach athlete identity + stores token.
3) **Roster auto-sync job** runs after coach login and populates:
   - `athletes` rows for roster athletes
   - `coach_roster_members` mapping
4) **Coach dashboard loads** and athlete dropdown only shows roster athletes.
5) **Athlete login works** and athlete can only see their own data.
6) **HTMX partial refresh works** for at least one panel (e.g., workouts table).
7) **Sync Now** button enqueues a job and UI shows status + updates panels on completion.
8) **Daily scheduler** enqueues jobs (not executing heavy work inside the web process).
9) Streamlit app still runs unchanged as fallback.

## Notes / Future Enhancements
- Add a separate `users` table if you need multiple identities per athlete or richer permissioning.
- Consider Redis only if you need high throughput, pub/sub, or complex background workflows.
