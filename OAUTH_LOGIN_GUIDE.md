# OAuth Login Guide for Test Automation Helper

## Overview

The test automation helper now includes OAuth login functionality so you can authenticate with TrainingPeaks directly from the command line - no need to run the full Streamlit app!

## Quick Start

```bash
# Activate virtual environment (if not already activated)
.venv\Scripts\Activate.ps1

# Run the helper
python test_automation_helper.py

# Choose option 1: Login with TrainingPeaks (OAuth)
```

## Step-by-Step OAuth Login

### 1. Start the OAuth Flow

```
Enter your choice (0-12): 1
```

### 2. Select Authorization Role

You'll be asked to choose between:
- **Athlete**: For accessing your own data
- **Coach**: For accessing athlete roster and coaching features (recommended for testing daily job)

```
Select authorization role:
  1. Athlete (athlete:profile, metrics:read, workouts)
  2. Coach (coach:athletes, metrics:read, workouts)

Enter choice (1-2) [default: 2]: 2
```

**For testing the daily job, choose Coach (option 2)**

### 3. Browser Opens Automatically

The helper will:
1. Generate an authorization URL
2. Open your default browser to TrainingPeaks login page
3. If browser doesn't open, copy/paste the URL shown

### 4. Complete TrainingPeaks Login

In the browser:
1. Enter your TrainingPeaks Sandbox credentials
2. Click "Authorize" to grant access
3. You'll be redirected to `http://localhost:8501/?code=...&state=...`
4. **The page won't load** (that's OK! We don't need the Streamlit app running)

### 5. Copy the Redirect URL

**IMPORTANT:** Copy the **ENTIRE URL** from your browser address bar.

It will look like:
```
http://localhost:8501/?code=abc123def456...&state=xyz789...
```

### 6. Paste URL Back in Terminal

```
Paste the redirect URL here: http://localhost:8501/?code=abc123...&state=xyz789...
```

Press Enter.

### 7. Token Exchange & Verification

The helper will:
- Extract the authorization code
- Exchange it for an access token
- Store the token in the database
- Verify it works by fetching your profile

You should see:
```
✓ Authorization code extracted: abc123def456...
📡 Exchanging code for access token...
✓ Token received successfully!
✓ Using athlete: Demo Athlete (ID: 1)
✓ Token stored in database
🔍 Verifying token by fetching profile...
✓ Profile verified: Your Name
✓ Athlete record updated

======================================================================
✓ OAUTH LOGIN COMPLETE
======================================================================

You can now use option 5 to run the daily job!
```

## After Login

Once authenticated, you can:

### Check Token Status (Option 2)
```
Enter your choice (0-12): 2
```

This shows:
- Whether you have a valid coach token
- When it expires
- What scopes it has
- If it's expired (needs refresh)

### Run Daily Job (Option 5)
```
Enter your choice (0-12): 5
```

Now that you're authenticated, the daily job can:
- Fetch data from TrainingPeaks
- Evaluate recovery alerts for all athletes
- Send email notifications

## Troubleshooting

### "No module named 'pydantic_settings'"

Make sure virtual environment is activated:
```bash
.venv\Scripts\Activate.ps1
python test_automation_helper.py
```

### "Token exchange failed"

Common causes:
1. **Authorization code expired** (60-minute timeout)
   - Solution: Start OAuth flow again

2. **Wrong redirect URI**
   - Check `.env` file: `TP_REDIRECT_URI=http://localhost:8501/`
   - Must match exactly (including trailing slash)

3. **Invalid credentials**
   - Verify `TP_CLIENT_ID` and `TP_CLIENT_SECRET` in `.env`
   - Make sure using Sandbox credentials

### "No authorization code found in URL"

Make sure you copied the **complete URL** including:
- `http://localhost:8501/`
- `?code=...`
- `&state=...`

Example of correct URL:
```
http://localhost:8501/?code=abc123&state=xyz789
```

### Browser doesn't open

If browser doesn't open automatically:
1. Copy the URL shown in terminal
2. Paste it in your browser manually
3. Continue with login

### "OAuth state mismatch"

This is usually safe to ignore. The code will proceed anyway.

### "Profile fetch failed"

Token may still be valid even if profile fetch fails. Try using the token (option 5 to run daily job).

## Token Management

### View Current Token Status

```
Enter your choice (0-12): 2
```

Shows:
- Athlete ID
- Expiration time
- Scopes
- Validity status

### Token Expiration

Tokens expire after a certain period. When expired:
- The helper will show "⚠️ Token is EXPIRED"
- The token will be auto-refreshed on first use
- Or you can re-authenticate (option 1)

### Multiple Tokens

If you have multiple tokens in the database:
- Option 2 shows all tokens
- The system uses the most recent coach token
- Coach token = has "coach:athletes" scope

## Re-Authentication

To get a new token:
```
Enter your choice (0-12): 1
```

If you already have a token:
```
⚠️  You already have a coach token!
Expires: 2025-10-27 15:30:00+00:00
Scope: coach:athletes metrics:read workouts:read

Re-authorize anyway? (y/n): y
```

Choose `y` to get a fresh token.

## Testing Flow Example

Complete workflow to test the daily job:

```bash
# 1. Start helper
python test_automation_helper.py

# 2. Login (option 1)
Enter your choice: 1
[Follow OAuth steps above]

# 3. Check token (option 2)
Enter your choice: 2
# Verify coach token is present

# 4. Check athletes (option 8)
Enter your choice: 8
# See what athletes are in database

# 5. Run daily job (option 5)
Enter your choice: 5
# Watch it ingest data and evaluate alerts!

# 6. Exit (option 0)
Enter your choice: 0
```

## Menu Quick Reference

After logging in, all options are available:

**OAuth & Auth:**
- 1: Login with TrainingPeaks
- 2: Check token status

**Live Email Tests:**
- 3: Send test email with generated data
- 4: Send test email with existing athlete

**Scheduler Tests:**
- 5: Run daily job manually ← **USE THIS AFTER LOGIN**
- 6: Multi-day time simulation
- 7: Mock time progression test

**Manual Operations:**
- 8: List all athletes
- 9: Check recovery alert for athlete
- 10: Create test data

**Scheduler Control:**
- 11: Start background scheduler
- 12: Check scheduler status

## Configuration

OAuth settings in `.env`:
```properties
# TrainingPeaks Sandbox OAuth
TP_CLIENT_ID=usa-triathlon-project-podium
TP_CLIENT_SECRET=your_secret_here
TP_AUTH_BASE=https://oauth.sandbox.trainingpeaks.com
TP_API_BASE=https://api.sandbox.trainingpeaks.com
TP_REDIRECT_URI=http://localhost:8501/
TP_SCOPE=athlete:profile coach:athletes events:read metrics:read...
```

**Important:**
- `TP_REDIRECT_URI` must be exactly `http://localhost:8501/` (with trailing slash)
- Use Sandbox credentials for testing

## Benefits

✅ **No Streamlit needed** - Test from command line
✅ **Quick authentication** - OAuth flow in terminal
✅ **Token verification** - Automatic profile check
✅ **Easy testing** - Run daily job immediately after login
✅ **Status monitoring** - Check token validity anytime

## Next Steps

After successful login:
1. Use option 5 to test the daily job
2. Use option 8 to see your athlete roster
3. Use option 9 to check recovery alerts for specific athletes
4. Use option 11 to test the background scheduler

---

**Last Updated:** October 26, 2025
**Requires:** Virtual environment activated, valid TrainingPeaks Sandbox credentials
