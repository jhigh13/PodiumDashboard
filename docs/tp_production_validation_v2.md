# TrainingPeaks API Integration — Project Podium Dashboard

## 1. Purpose of the Integration

Podium Dashboard is a coaching and athlete performance tool for USA Triathlon. It uses the TrainingPeaks API to pull workout and daily metrics data for elite triathletes, then combines that with World Triathlon race results to analyze training inputs alongside race outcomes.

Core goals:

- Ingest daily training load, workout details, and physiological metrics from TrainingPeaks.
- Compare race-day execution between two races for the same athlete (e.g., run pace and heart rate traces from Race A vs. Race B).
- Enable coach workflows that view and analyze training-to-performance signals across all roster athletes.

---

## 2. High-Level Architecture

```
Coach/Athlete logs in
        │
        ▼
OAuth 2.0 Authorization Code Flow
        │
        ▼
TrainingPeaks OAuth  ──►  Access + Refresh Token (stored in DB)
        │
        ▼
   TP API Client
   ┌────┴──────────────────────────────────┐
   │  GET /v1/coach/athletes               │  Coach roster sync
   │  GET /v2/workouts/{id}/{start}/{end}  │  Workout list ingest
   │  GET /v2/metrics/{id}/{start}/{end}   │  Daily metrics ingest
   │  GET /v2/workouts/{id}/{workoutId}    │  Single workout metadata
   │  GET /v2/workouts/{id}/id/{wid}/details│  Time-series channels
   │  GET /v2/workouts/{id}/wod/file/{wid} │  Structured file download
   └───────────────────────────────────────┘
        │
        ▼
   Podium Database  ◄──  World Triathlon Results DB
        │
        ▼
   Race-vs-Training Analysis Views (FastAPI + HTMX)
```

The application is a **FastAPI + HTMX** web app served by Uvicorn. It stores TrainingPeaks data in a PostgreSQL database and renders analysis views as server-side HTML fragments.

---

## 3. TrainingPeaks API Interaction Details

### 3.1 Authentication

We use the standard OAuth 2.0 Authorization Code flow per the [Partners API docs](https://github.com/TrainingPeaks/PartnersAPI/wiki). Tokens are stored per-athlete, refreshed automatically before expiry, and purged on failure so the user is prompted to re-authorize.

### 3.2 Scopes

**Requested:**

- **Athlete login:** `athlete:profile`, `metrics:read`, `workouts:read`, `workouts:details`, `workouts:wod`
- **Coach login (adds):** `coach:athletes`

**Not requested:** `events:read`, `events:write`, `file:write`, `metrics:write`, `workouts:plan` — we have no use case for these.

Coach tokens access all athlete-scoped endpoints for any athlete on the coach's roster.

### 3.3 Endpoints Used

| Endpoint | Method |
|----------|--------|
| `/v1/coach/athletes` | GET |
| `/v2/workouts/{athleteId}/{startDate}/{endDate}` | GET |
| `/v2/metrics/{athleteId}/{startDate}/{endDate}` | GET |
| `/v2/workouts/{athleteId}/{workoutId}` | GET |
| `/v2/workouts/{athleteId}/id/{workoutId}/details` | GET |
| `/v2/workouts/{athleteId}/wod/file/{workoutId}/?format={fmt}` | GET |

All requests are read-only GETs. Date-range queries are segmented into ≤45-day windows.

### 3.4 User-Agent Header

`User-Agent` is set on every HTTP request to TrainingPeaks (OAuth token calls and all API reads). The value is configurable via environment variable — default `PodiumDashboard/1.0`. Happy to adjust the format to whatever you require.

### 3.5 Data Handling

- Time-series payloads are cached locally after the first fetch to minimize repeat API calls.
- We never write, modify, or delete data in TrainingPeaks.

### 3.6 Premium vs. Basic Athlete Handling

Our target users are elite triathletes on a USA Triathlon coaching roster — all premium accounts. That said, we handle non-premium cases gracefully:

- **metrics:read** — 403 responses indicating "premium only" are detected and that athlete's metrics are skipped for the session (no retries).
- **workouts:details** — 403 is surfaced to the user as "access denied"; we do not call this endpoint for athletes we've identified as non-premium.
- **workouts:read** — works for both premium and basic athletes; restricted fields (TSS, IF, NP, etc.) return null for basic athletes, which we handle as missing data.

---

## 4. Key Performance Use Cases

### A. Race-to-Race Comparison (Same Athlete)

1. The coach or athlete selects two World Triathlon race results from the comparison view.
2. The app locates TrainingPeaks workouts around each race date (±1 day) and filters by sport (run, bike, or swim).
3. For each selected workout, the app fetches the time-series detail channels and charts:
   - **Run**: pace over time (min/mi) + heart rate
   - **Bike**: power over time (W) + heart rate
   - **Swim**: pace over time (min/100 m)
4. Both races are overlaid on a single Plotly chart for direct visual comparison of race-day execution patterns.

### B. Coach Roster Workflows

1. The coach authenticates with `coach:athletes` scope.
2. Roster athletes are pulled from TrainingPeaks and mapped to local athlete records.
3. The coach switches between athletes in the dashboard and can run:
   - Training load and compliance views (workouts vs. plan)
   - Recovery alert analysis (baseline deviation flagging)
   - Race-vs-training comparison views per athletes

---

## 5. Branding

All references to "TrainingPeaks" in the app are plain-text product identification (button labels, section headers). No logos, brand imagery, or assets are used. We follow the TrainingPeaks Media Kit guidelines.

---

## Contact

**John High**
USA Triathlon
john.high@usatriathlon.org
