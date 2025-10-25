import requests
from datetime import datetime, date, timedelta, timezone
from app.utils.settings import settings
from app.services.tokens import get_token, store_token, delete_token, find_coach_token
from app.services.athletes import get_or_create_demo_athlete, get_athlete_by_id
from app.auth.oauth import refresh_token as oauth_refresh

API_BASE = settings.tp_api_base.rstrip('/')

class TrainingPeaksAPI:
    def __init__(self, athlete_id: int):
        self.athlete_id = athlete_id
        self._using_coach_token = False  # Track if we're using a coach token fallback

    def _get_access_token(self):
        token_row = get_token(self.athlete_id)
        if not token_row:
            # Fallback: use a coach token (for coach-mode operations on roster athletes)
            coach_tok = find_coach_token()
            if not coach_tok:
                raise RuntimeError("No OAuth token stored for this athlete and no coach token found. Connect TrainingPeaks as Coach.")
            token_row = coach_tok
            self._using_coach_token = True
        else:
            self._using_coach_token = False
        # simplistic expiry check (1 min buffer)
        try:
            if token_row.expires_at and (token_row.expires_at - timedelta(minutes=1)) < datetime.now(timezone.utc):
                new_token = oauth_refresh(token_row.refresh_token)
                # If this is a coach token applied to a roster athlete, persist refresh under the owning athlete id
                # by simply updating the same record's owning athlete id. We will store it under the original token owner.
                # For simplicity, keep it associated with the original token's athlete_id.
                store_token(token_row.athlete_id, new_token)
                # Reload whichever token we refreshed (coach or athlete-specific)
                token_row = get_token(token_row.athlete_id)
            if not token_row or not token_row.access_token:
                raise RuntimeError("Stored OAuth token missing access_token; please re-authorize.")
            return token_row.access_token
        except RuntimeError:
            # purge invalid token so UI logic can show re-auth button immediately
            # Only delete if it was the athlete's own token; preserve coach token if used
            if token_row and token_row.athlete_id == self.athlete_id:
                delete_token(self.athlete_id)
            raise

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
            "User-Agent": "podium-dashboard/0.1"
        }

    def fetch_workouts(self, start_date: date, end_date: date, tp_athlete_id: int | None = None):
        # TrainingPeaks workouts endpoints are v2 and use path segments.
        # Coach tokens WITH workouts:read scope CAN use athlete-scoped endpoints
        def call_segment(s: date, e: date):
            if tp_athlete_id:
                # Use athlete-scoped endpoint (works for both athlete tokens and coach tokens with proper permissions)
                url = f"{API_BASE}/v2/workouts/{tp_athlete_id}/{s.isoformat()}/{e.isoformat()}"
                headers = self._headers()
                r = requests.get(url, headers=headers, timeout=30)
                
                # DEBUG: Log details on non-200 responses
                if r.status_code != 200:
                    error_body = ""
                    try:
                        error_body = r.text[:500]
                    except:
                        pass
                    print(f"\n=== WORKOUTS API DEBUG ===")
                    print(f"URL: {url}")
                    print(f"Status: {r.status_code}")
                    print(f"Using coach token: {self._using_coach_token}")
                    print(f"Headers (token masked): {dict((k, v[:20]+'...' if k == 'Authorization' else v) for k,v in headers.items())}")
                    print(f"Response body: {error_body}")
                    print(f"========================\n")
                
                if r.status_code == 403:
                    # Get token details for debugging
                    token_row = get_token(self.athlete_id) or find_coach_token()
                    scope = getattr(token_row, 'scope', 'unknown') if token_row else 'no token'
                    raise RuntimeError(
                        f"403 Forbidden for athlete {tp_athlete_id}. "
                        f"Using coach token: {self._using_coach_token}, Token scopes: {scope}. "
                        f"URL: {url}"
                    )
                r.raise_for_status()
                return r.json() or []
            else:
                url = f"{API_BASE}/v2/workouts/{s.isoformat()}/{e.isoformat()}"
                r = requests.get(url, headers=self._headers(), timeout=30)
                r.raise_for_status()
                return r.json() or []

        # Try full range, fall back to segmentation on 400/403
        # TrainingPeaks has a 45-day maximum for date range queries
        try:
            return call_segment(start_date, end_date)
        except requests.HTTPError as e:
            status = getattr(e.response, 'status_code', None)
            if status not in (400, 403):
                raise
            # Segment into 45-day windows (TP's max) and merge
            out = []
            cur_end = end_date
            step = 45  # TrainingPeaks maximum
            while cur_end >= start_date:
                cur_start = max(start_date, cur_end - timedelta(days=step - 1))
                seg = call_segment(cur_start, cur_end)
                out.extend(seg)
                if cur_start == start_date:
                    break
                cur_end = cur_start - timedelta(days=1)
            return out

    def fetch_daily_metrics_range(self, start_date: date, end_date: date, tp_athlete_id: int | None = None):
        # Coach tokens WITH metrics:read scope CAN use athlete-scoped endpoints
        def call_segment(s: date, e: date):
            if tp_athlete_id:
                # Use athlete-scoped endpoint (works for both athlete tokens and coach tokens with proper permissions)
                url = f"{API_BASE}/v2/metrics/{tp_athlete_id}/{s.isoformat()}/{e.isoformat()}"
                headers = self._headers()
                r = requests.get(url, headers=headers, timeout=30)
                
                # DEBUG: Log details on non-200/404 responses
                if r.status_code not in (200, 404):
                    error_body = ""
                    try:
                        error_body = r.text[:500]
                    except:
                        pass
                    print(f"\n=== METRICS API DEBUG ===")
                    print(f"URL: {url}")
                    print(f"Status: {r.status_code}")
                    print(f"Using coach token: {self._using_coach_token}")
                    print(f"Headers (token masked): {dict((k, v[:20]+'...' if k == 'Authorization' else v) for k,v in headers.items())}")
                    print(f"Response body: {error_body}")
                    print(f"========================\n")
                
                if r.status_code == 404:
                    return []
                if r.status_code == 403:
                    # Get token details for debugging
                    token_row = get_token(self.athlete_id) or find_coach_token()
                    scope = getattr(token_row, 'scope', 'unknown') if token_row else 'no token'
                    raise RuntimeError(
                        f"403 Forbidden for athlete {tp_athlete_id}. "
                        f"Using coach token: {self._using_coach_token}, Token scopes: {scope}. "
                        f"URL: {url}"
                    )
                r.raise_for_status()
                return r.json() or []
            else:
                url = f"{API_BASE}/v2/metrics/{s.isoformat()}/{e.isoformat()}"
                r = requests.get(url, headers=self._headers(), timeout=30)
                if r.status_code == 404:
                    return []
                r.raise_for_status()
                return r.json() or []

        # TrainingPeaks has a 45-day maximum for date range queries
        try:
            return call_segment(start_date, end_date)
        except requests.HTTPError as e:
            status = getattr(e.response, 'status_code', None)
            if status not in (400, 403):
                raise
            out = []
            cur_end = end_date
            step = 45  # TrainingPeaks maximum
            while cur_end >= start_date:
                cur_start = max(start_date, cur_end - timedelta(days=step - 1))
                seg = call_segment(cur_start, cur_end)
                out.extend(seg)
                if cur_start == start_date:
                    break
                cur_end = cur_start - timedelta(days=1)
            return out

    def fetch_coach_athletes(self):
        """Fetch coach roster (requires coach:athletes scope).

        Returns list of athletes with keys like Id, FirstName, LastName, Email, etc.
        """
        url = f"{API_BASE}/v1/coach/athletes"
        r = requests.get(url, headers=self._headers(), timeout=30)
        # If scope is missing or user is not a coach, TP may return 403
        if r.status_code == 403:
            # Surface a clear message to the caller
            raise RuntimeError("TrainingPeaks API returned 403 for coach roster. Ensure the account is a coach and the token has coach:athletes scope. Re-authorize if needed.")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json() or []

    def fetch_workout_details(self, workout_id: str, tp_athlete_id: int | None = None):
        if not workout_id:
            return None
        if tp_athlete_id:
            url = f"{API_BASE}/v2/workouts/{tp_athlete_id}/{workout_id}"
        else:
            url = f"{API_BASE}/v2/workouts/{workout_id}"
        r = requests.get(url, headers=self._headers(), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return None


def get_api(athlete_id: int | None = None):
    """Return an API client bound to a specific athlete id.

    If athlete_id is None, falls back to the demo athlete.
    """
    if athlete_id is None:
        athlete = get_or_create_demo_athlete()
        return TrainingPeaksAPI(athlete.id)
    athlete = get_athlete_by_id(athlete_id) or get_or_create_demo_athlete()
    return TrainingPeaksAPI(athlete.id)
