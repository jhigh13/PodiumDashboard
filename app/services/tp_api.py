import requests
from datetime import datetime, date, timedelta, timezone
from app.utils.settings import settings
from app.services.tokens import get_token, store_token, delete_token, find_coach_token
from app.services.athletes import get_or_create_demo_athlete, get_athlete_by_id
from app.auth.oauth import refresh_token as oauth_refresh

API_BASE = settings.tp_api_base.rstrip('/')


# In-process caches to avoid repeatedly calling premium/forbidden endpoints.
_NON_PREMIUM_METRICS_TP_IDS: set[int] = set()
_METRICS_PREMIUM_WARNED_TP_IDS: set[int] = set()

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
                if int(tp_athlete_id) in _NON_PREMIUM_METRICS_TP_IDS:
                    return []
                # Use athlete-scoped endpoint (works for both athlete tokens and coach tokens with proper permissions)
                url = f"{API_BASE}/v2/metrics/{tp_athlete_id}/{s.isoformat()}/{e.isoformat()}"
                headers = self._headers()
                r = requests.get(url, headers=headers, timeout=30)

                # Handle premium-only restriction early to avoid noisy debug logging.
                if r.status_code == 403:
                    response_text = (r.text or "").lower()
                    if "premium" in response_text:
                        _NON_PREMIUM_METRICS_TP_IDS.add(int(tp_athlete_id))
                        if int(tp_athlete_id) not in _METRICS_PREMIUM_WARNED_TP_IDS:
                            _METRICS_PREMIUM_WARNED_TP_IDS.add(int(tp_athlete_id))
                            print(f"⚠️  Metrics unavailable (non-premium) for TP athlete {tp_athlete_id}; skipping metrics.")
                        raise RuntimeError(
                            f"403 Forbidden: This data is only available for premium athletes (athlete {tp_athlete_id})"
                        )
                
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
                    response_text = (r.text or "").lower()
                    if "premium" in response_text:
                        _NON_PREMIUM_METRICS_TP_IDS.add(int(tp_athlete_id))
                        raise RuntimeError(
                            f"403 Forbidden: This data is only available for premium athletes (athlete {tp_athlete_id})"
                        )
                    # Other 403 errors (token/permission issues)
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

    def fetch_workout_available_formats(self, workout_id: str, tp_athlete_id: int | None = None):
        """Check which file formats are available for a workout.
        
        Args:
            workout_id: The TrainingPeaks workout ID
            tp_athlete_id: Optional athlete ID for athlete-scoped endpoint
            
        Returns:
            list of available format strings (e.g., ['fit', 'json', 'mrc'])
            Empty list if workout has no structured data
            
        Raises:
            requests.HTTPError: For API errors
        """
        if not workout_id:
            return []
        
        # Use athlete-scoped endpoint if tp_athlete_id provided
        if tp_athlete_id and self._using_coach_token:
            url = f"{API_BASE}/v2/workouts/{tp_athlete_id}/wod/file/{workout_id}/"
        else:
            url = f"{API_BASE}/v2/workouts/wod/file/{workout_id}/"
        
        # Make HEAD request to check available formats
        r = requests.head(url, headers=self._headers(), timeout=10)
        
        if r.status_code == 404:
            # Workout has no structured data
            return []
        elif r.status_code == 200:
            # Check Content-Type or Accept header for available formats
            # TrainingPeaks usually returns available formats in headers
            # For now, return common formats - could be enhanced
            return ['fit', 'json', 'mrc', 'erg', 'zwo']
        else:
            r.raise_for_status()
            return []

    def fetch_workout_file(self, workout_id: str, file_format: str = 'fit', tp_athlete_id: int | None = None):
        """Download a structured workout file in the specified format.
        
        Args:
            workout_id: The TrainingPeaks workout ID
            file_format: File format to download (fit, erg, mrc, zwo, json). Default is 'fit'
            tp_athlete_id: Optional athlete ID for athlete-scoped endpoint
            
        Returns:
            dict with:
                - 'content': bytes of the file (for binary formats) or dict (for json)
                - 'filename': suggested filename from Content-Disposition header
                - 'format': the requested format
            
        Raises:
            RuntimeError: If workout has no structure or format is invalid
            requests.HTTPError: For other API errors
        """
        if not workout_id:
            raise ValueError("workout_id is required")
        
        # Validate format
        valid_formats = ['fit', 'erg', 'mrc', 'zwo', 'json']
        file_format = file_format.lower()
        if file_format not in valid_formats:
            raise ValueError(f"Invalid format '{file_format}'. Must be one of: {', '.join(valid_formats)}")
        
        # Build URL - use athlete-scoped endpoint if tp_athlete_id provided
        if tp_athlete_id and self._using_coach_token:
            url = f"{API_BASE}/v2/workouts/{tp_athlete_id}/wod/file/{workout_id}/?format={file_format}"
        else:
            # Athlete accessing their own workout or demo
            url = f"{API_BASE}/v2/workouts/wod/file/{workout_id}/?format={file_format}"
        
        # Make request
        r = requests.get(url, headers=self._headers(), timeout=30)
        
        # Handle error responses
        if r.status_code == 400:
            error_msg = r.text or 'Unknown error'
            if 'no structure' in error_msg.lower():
                raise RuntimeError(f"Workout {workout_id} has no structure and cannot be exported")
            elif 'cannot be exported' in error_msg.lower():
                raise RuntimeError(f"Workout {workout_id} cannot be exported to format '{file_format}'")
            else:
                raise RuntimeError(f"Bad request: {error_msg}")
        
        if r.status_code == 404:
            raise RuntimeError(f"Workout {workout_id} not found")
        
        r.raise_for_status()
        
        # Extract filename from Content-Disposition header
        filename = None
        content_disposition = r.headers.get('Content-Disposition', '')
        if 'filename=' in content_disposition:
            # Parse filename from header like: attachment; filename="workout.fit"
            import re
            match = re.search(r'filename="?([^"]+)"?', content_disposition)
            if match:
                filename = match.group(1)
        
        # If no filename from header, create a default one
        if not filename:
            filename = f"workout_{workout_id}.{file_format}"
        
        # For JSON format, parse and return as dict
        if file_format == 'json':
            return {
                'content': r.json(),
                'filename': filename,
                'format': file_format,
                'workout_id': workout_id
            }
        
        # For binary formats (fit, erg, mrc, zwo), return raw bytes
        return {
            'content': r.content,
            'filename': filename,
            'format': file_format,
            'workout_id': workout_id
        }

    def fetch_workout_time_series(self, workout_id: str, tp_athlete_id: int | None = None):
        """Fetch detailed workout data including time series channels.
        
        This endpoint returns comprehensive workout data including:
        - Time series data with channels (HR, power, cadence, elevation, etc.)
        - Workout statistics (averages, maximums, TSS, IF, etc.)
        - Lap statistics
        - Swim statistics (if applicable)
        
        This is useful for workouts that don't have structured workout files,
        as it provides second-by-second data from the actual workout file.
        
        Args:
            workout_id: The TrainingPeaks workout ID
            tp_athlete_id: Optional athlete ID for athlete-scoped endpoint
            
        Returns:
            dict with:
                - 'WorkoutId': The workout ID
                - 'WorkoutChannels': Time series data with Channels list and Data array
                - 'WorkoutStats': Overall workout statistics
                - 'LapStats': Per-lap statistics (if available)
                - 'SwimStats': Swim-specific data (if swim workout)
            
        Raises:
            RuntimeError: If workout not found
            requests.HTTPError: For other API errors
        """
        if not workout_id:
            raise ValueError("workout_id is required")
        
        # Build URL - use athlete-scoped endpoint if tp_athlete_id provided
        if tp_athlete_id:
            # Coach accessing roster athlete's workout details
            url = f"{API_BASE}/v2/workouts/{tp_athlete_id}/id/{workout_id}/details"
        else:
            # Athlete accessing their own workout details
            url = f"{API_BASE}/v2/workouts/id/{workout_id}/details"
        
        # Make request (increased timeout for large time series data)
        # Use retry logic for timeout-prone endpoint
        headers = self._headers()
        max_retries = 1  # Reduced retries since connection errors happen immediately
        timeout = 120  # 2 minutes - anything larger hits server-side limits
        
        print(f"  ⏳ Requesting time series data (timeout: {timeout}s)...")
        
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                r = requests.get(url, headers=headers, timeout=timeout)
                print(f"  ✓ Received response (status: {r.status_code})")
                break  # Success, exit retry loop
            except requests.exceptions.ReadTimeout:
                if attempt < max_retries:
                    print(f"  ⏱️  Timeout after {timeout}s, retrying (attempt {attempt + 2}/{max_retries + 1})...")
                    timeout += 60  # Add 60 seconds for each retry
                    continue
                else:
                    # Final attempt failed
                    raise RuntimeError(
                        f"Workout {workout_id} timed out after {max_retries + 1} attempts ({timeout}s total). "
                        f"This workout has extremely large time series data. "
                        f"Try Option 9 (FIT file download) instead."
                    )
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                last_error = e
                if attempt < max_retries:
                    print(f"  🔌 Connection error, retrying (attempt {attempt + 2}/{max_retries + 1})...")
                    continue
                else:
                    # Server closed connection - likely data too large for API endpoint
                    raise RuntimeError(
                        f"Workout {workout_id} connection closed by server after {attempt + 1} attempt(s). "
                        f"Time series data is too large for this API endpoint. "
                        f"Use Option 9 (FIT file download) instead - FIT files handle large datasets better."
                    )
        
        # DEBUG: Log details on non-200 responses
        if r.status_code != 200:
            error_body = ""
            try:
                error_body = r.text[:500]
            except:
                pass
            print(f"\n=== WORKOUT DETAILS API DEBUG ===")
            print(f"URL: {url}")
            print(f"Status: {r.status_code}")
            print(f"Using coach token: {self._using_coach_token}")
            print(f"Athlete ID provided: {tp_athlete_id}")
            print(f"Workout ID: {workout_id}")
            print(f"Headers (token masked): {dict((k, v[:20]+'...' if k == 'Authorization' else v) for k,v in headers.items())}")
            print(f"Response body: {error_body}")
            print(f"================================\n")
        
        # Handle 204 No Content - workout exists but has no time series data
        if r.status_code == 204:
            raise RuntimeError(
                f"Workout {workout_id} has no time series data available. "
                f"This can happen if: (1) workout wasn't uploaded with a device file, "
                f"(2) only manual workout entry exists, or (3) data was deleted."
            )
        
        # Handle error responses
        if r.status_code == 404:
            raise RuntimeError(f"Workout {workout_id} not found or no details available")
        
        if r.status_code == 403:
            raise RuntimeError(f"Access denied for workout {workout_id}. Ensure token has workouts:details scope")
        
        r.raise_for_status()
        
        # Parse and return JSON data
        try:
            data = r.json()
            return data
        except ValueError as e:
            raise RuntimeError(f"Failed to parse workout details: {e}") from e


def get_api(athlete_id: int | None = None):
    """Return an API client bound to a specific athlete id.

    If athlete_id is None, falls back to the demo athlete.
    """
    if athlete_id is None:
        athlete = get_or_create_demo_athlete()
        return TrainingPeaksAPI(athlete.id)
    athlete = get_athlete_by_id(athlete_id) or get_or_create_demo_athlete()
    return TrainingPeaksAPI(athlete.id)
