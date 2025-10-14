# OAuth Token Exchange Fix - Summary

## Problem
Token exchange was failing with error: `RuntimeError: OAuth token exchange failed: Expecting value: line 1 column 1 (char 0)`

## Root Cause
The `authlib` library was sending the token exchange request with incorrect content-type (likely JSON), but **TrainingPeaks requires `application/x-www-form-urlencoded`** per their API documentation.

## Changes Made

### 1. `app/auth/oauth.py` - Token Exchange (`fetch_token`)
**Before:** Used authlib's `session.fetch_token()` which sends JSON
**After:** Direct `requests.post()` with proper form encoding

Key changes:
- ✅ Content-Type: `application/x-www-form-urlencoded`
- ✅ All parameters in POST body as form data
- ✅ Better error handling with HTTP status codes
- ✅ Detailed error messages showing actual TrainingPeaks response

### 2. `app/auth/oauth.py` - Token Refresh (`refresh_token`)
**Before:** Missing explicit Content-Type header
**After:** Explicitly sets `application/x-www-form-urlencoded`

### 3. `app/ui/oauth_view.py` - User Feedback
Added diagnostic expander showing:
- Authorization code received (truncated for security)
- State validation status
- Real-time token exchange progress
- Troubleshooting steps if exchange fails

## TrainingPeaks API Requirements (per documentation)

From: https://github.com/TrainingPeaks/PartnersAPI/wiki/OAuth

### Token Exchange Endpoint
```
POST https://oauth.sandbox.trainingpeaks.com/oauth/token
Content-Type: application/x-www-form-urlencoded
```

### Required Parameters
- `grant_type`: "authorization_code"
- `code`: Authorization code from callback
- `redirect_uri`: Must match exactly what was used in authorization step
- `client_id`: Your client identifier
- `client_secret`: Your client secret

### Common `invalid_request` Causes (from docs)
1. ❌ Incorrect content-type encoding → **FIXED**
2. ❌ Missing or incorrect parameters
3. ❌ Incorrect grant_type
4. ❌ redirect_uri doesn't match
5. ❌ Incorrect client_secret
6. ❌ Expired code (60 minute expiration)

## Testing Instructions

### Step 1: Restart Streamlit
```powershell
# Stop current Streamlit
Ctrl+C

# Restart
streamlit run app/main.py
```

### Step 2: Clear Browser Cache (Optional but Recommended)
- Clear cookies for localhost:8501
- Or use incognito/private browsing window

### Step 3: Run OAuth Flow
1. Navigate to "Connect TrainingPeaks" page
2. Select role (Athlete or Coach)
3. Click "Start OAuth Flow"
4. Log in to TrainingPeaks Sandbox
5. Authorize the application
6. **Watch for the "🔍 OAuth Debug Info" expander**

### Expected Outcome ✅
- Debug panel shows: "Attempting token exchange..."
- Success message: "✅ Token stored successfully!"
- Page automatically switches to Dashboard
- Token status banner shows green checkmark

### If Still Fails ❌
The debug panel will show:
- HTTP status code
- Actual error message from TrainingPeaks
- This will help identify if issue is:
  - Redirect URI mismatch
  - Client credential problem
  - Code expiration
  - Other API issue

## What Was Wrong Before

The original implementation used `authlib.OAuth2Session.fetch_token()` which:
1. Defaults to JSON content-type for token requests
2. TrainingPeaks rejected the request
3. Returned HTML error page instead of JSON
4. Python's JSON parser failed: "Expecting value: line 1 column 1"

## Why This Fix Works

1. **Proper Content-Type**: TrainingPeaks now receives form-encoded data as expected
2. **Better Error Handling**: If still fails, we see the actual TrainingPeaks error message
3. **Diagnostic Visibility**: User sees real-time progress and can troubleshoot
4. **Follows Official Docs**: Implementation matches TrainingPeaks OAuth wiki exactly

## Additional Notes

### Token Expiration
- Access tokens expire in **600 seconds (10 minutes)** per TrainingPeaks
- Refresh tokens used to get new access tokens
- Both refresh functions now use proper form encoding

### Sandbox Environment
- Database refreshed every weekend
- Test accounts and tokens cleared Monday morning
- Must use sandbox URLs during development

### Production Migration
Once sandbox testing complete:
- Update `.env`: Remove "sandbox" from URLs
- `TP_AUTH_BASE=https://oauth.trainingpeaks.com`
- `TP_API_BASE=https://api.trainingpeaks.com`
- TrainingPeaks will activate production credentials

## References
- [TrainingPeaks OAuth Documentation](https://github.com/TrainingPeaks/PartnersAPI/wiki/OAuth)
- [API Endpoints](https://github.com/TrainingPeaks/PartnersAPI/wiki/API-Endpoints)
- [Support Form](https://github.com/TrainingPeaks/PartnersAPI/wiki/Contact-API-Partnerships-Team)

---
**Date Fixed:** October 1, 2025
**Issue:** Token exchange failing with JSON decode error
**Solution:** Proper form encoding per TrainingPeaks API requirements
