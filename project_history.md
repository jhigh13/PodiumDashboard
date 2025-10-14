# Podium Dashboard Project History

## Project Goal
- Pull historical and daily TrainingPeaks data for an athlete.
- Track training volume vs. base metrics (RHR, HRV, sleep, body score, etc.).
- Detect potential overreaching/underrecovery each morning.
- Start in Sandbox with OAuth (token refresh hourly), then move to Production.

## Chosen Implementation (MVP)
**Option A:** Python + Streamlit (single-service MVP)

## Technology Stack
- Python, Streamlit UI
- Authlib (OAuth), requests
- APScheduler (daily jobs)
- Postgres (Supabase)
- SendGrid (email)
- Hosting: Streamlit app as a single service (containerized). Supabase for Postgres. Secrets via environment variables.

## Key Architecture Notes
- Streamlit app with coach authentication and role-based views
- OAuth connection to TrainingPeaks (sandbox in dev, prod later)
- Data ingestion: backfill service, daily sync
- Analytics: rolling baselines, ACWR, HRV/RHR/sleep scoring
- Notifications: in-app dashboard, daily email summary to head coach
- Storage tables: athletes, oauth_tokens, workouts, daily_metrics, aggregates, risk_assessments, email_log
- Scheduling: APScheduler cron, America/Denver timezone

## Security
- Use sandbox URLs during development
- Store secrets in env vars
- Register redirect URIs
- Weekly sandbox reset handling

## Phased Timeline
**Week 1:** Setup, OAuth flow, fetch athlete profile, backfill prototype

**Week 2:** Complete backfill, baseline computation, daily scheduler, dashboard

**Week 3:** Email summary, config UI, sandbox reset handling, deploy

**Week 4:** Move to prod endpoints, rotate secrets, QA, onboarding

## Decisions Confirmed
- Project name: Podium Dashboard
- Email provider: SendGrid
- Redirect URIs: 
  - Dev: http://localhost:8501/oauth/callback 
  - Prod: https://yourdomain.com/oauth/callback
- Daily job time: 7:30 AM America/Denver for all athletes
- Head coach email: john.high@usatriathlon.org
- Branding: N/A
- Secret rotation: Not required for MVP

---
Imported from previous workspace chat history on 2025-09-12.
