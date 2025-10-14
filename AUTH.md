# TrainingPeaks OAuth Flow

This document explains how the Podium Dashboard authenticates with the TrainingPeaks Sandbox API, how tokens are stored and refreshed, and how to troubleshoot common issues.

## Overview

Flow (Authorization Code Grant):
1. User selects role (Athlete or Coach) and clicks Start OAuth Flow in `Connect TrainingPeaks` page.
2. We build an authorization URL via `get_authorization_url` in `app/auth/oauth.py` with the selected scopes.
3. TrainingPeaks redirects back to the Streamlit redirect URI (default `http://localhost:8501/`) with `code` and `state`.
4. `oauth_view.render()` detects the `code` param and calls `fetch_token(code)`.
5. The returned token JSON is persisted via `store_token()` (table `oauth_tokens`).
6. Subsequent API calls retrieve the token via `get_token()`; if near expiry a refresh is attempted.

## Token Storage

Table: `oauth_tokens`
- `access_token`
- `refresh_token`
- `expires_at` (UTC, computed from `expires_in` field)
- `scope`

Only one row per athlete is kept (previous rows are deleted before insert).

## Refresh Logic

Implemented in `refresh_token()` (manual POST using `requests`). We avoid the implicit `authlib` helper for better diagnostic messages. On 200 OK we parse JSON and validate presence of `access_token`. On non-200 or non-JSON bodies we raise a `RuntimeError` with context.

In `TrainingPeaksAPI._get_access_token`:
- If `expires_at` is within 1 minute, we refresh.
- On any `RuntimeError` during refresh we delete the stored token (`delete_token`) so the UI can prompt the user to re-authorize.

## Scopes

Default athlete scopes (see `oauth_view`):
```
athlete:profile metrics:read workouts:read workouts:details
```
Coach scopes example:
```
coach:athletes metrics:read workouts:read workouts:details
```
Ensure your TrainingPeaks sandbox client is configured to allow all required scopes.

## Environment Variables

Set in `.env` (see `app/utils/settings.py`):
- `TP_CLIENT_ID`
- `TP_CLIENT_SECRET`
- `TP_AUTH_BASE` (default sandbox `https://oauth.sandbox.trainingpeaks.com`)
- `TP_API_BASE` (default sandbox `https://api.sandbox.trainingpeaks.com`)
- `TP_REDIRECT_URI` (must match client config, default `http://localhost:8501/`)

## Common Issues & Troubleshooting

| Symptom | Likely Cause | Resolution |
|---------|--------------|------------|
| `Token refresh failed (HTTP 400). Detail: {'error': 'invalid_grant'}` | Expired or revoked refresh token | Re-authorize via Connect page. |
| `Token refresh returned non-JSON body` | Service outage, HTML error page, proxy interference | Check network, confirm base URLs, re-authorize. Capture raw body if persists. |
| `No OAuth token stored` | User never completed flow | Start OAuth flow. |
| Repeated refresh attempts each request | System clock skew or incorrect `expires_in` assumption | Verify system time is correct; add logging of `expires_at` vs now. |

## Forcing Re-Authorization

Click the Re-authorize button in the Connect page, or manually delete the row from `oauth_tokens` for the athlete.

## Extending / Future Improvements
- Add proper logging (structured) around refresh attempts.
- Support multiple athletes / coaches by associating tokens with real user accounts.
- Implement background refresh (proactively refresh if < N minutes remaining).
- Add token encryption at rest.

## Manual cURL Debug (Optional)
```bash
curl -X POST \
  -d "grant_type=refresh_token" \
  -d "refresh_token=<REFRESH_TOKEN>" \
  -d "client_id=$TP_CLIENT_ID" \
  -d "client_secret=$TP_CLIENT_SECRET" \
  -H "Accept: application/json" \
  "$TP_AUTH_BASE/oauth/token"
```
Expect JSON with new `access_token`, `refresh_token`, and `expires_in`.

## Security Notes
- Do not commit real client secrets; use environment variables.
- Consider rotating client secret periodically.
- Add CSRF protection beyond basic state param if moving off Streamlit.

## Quick Checklist When Something Breaks
1. Verify environment variables loaded (print `settings.tp_client_id`).
2. Confirm redirect URI in TrainingPeaks developer portal matches `.env`.
3. Re-run auth flow; capture token JSON.
4. Attempt manual refresh via cURL; compare responses.
5. Inspect database `oauth_tokens` row for plausible `expires_at`.

---
This documentation reflects refactor as of latest commit adding robust refresh handling.
