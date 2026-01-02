"""
Interactive helper script for testing Podium Dashboard features.

This script provides an easy-to-use interface for:
- OAuth login with TrainingPeaks
- Token management
- Testing daily automation workflows
- Manual data operations
"""
import sys
import webbrowser
import urllib.parse
from datetime import date, datetime, timezone

from app.auth.oauth import get_authorization_url, fetch_token
from app.services.tokens import store_token, get_token, find_coach_token
from app.services.athletes import list_athletes, get_or_create_demo_athlete
from app.services.ingest import ingest_recent
from app.services.llm import llm_client
from app.services.compliance import get_compliance_for_day
from app.services.email import email_client
from app.services.fit_analysis import FitFileAnalyzer, analyze_fit_file, get_fit_summary
from app.utils.settings import settings
from app.utils.dates import get_effective_today
from app.data.db import get_session
from app.models.tables import Workout, DailyMetric, WorkoutCompliance, OAuthToken, Athlete
from sqlalchemy import delete, select, desc


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _has_any_token():
    """Check if there's any valid token (coach or athlete)."""
    with get_session() as session:
        stmt = select(OAuthToken).order_by(desc(OAuthToken.created_at)).limit(1)
        latest_token = session.execute(stmt).scalar_one_or_none()
        return latest_token is not None


def _get_current_athlete():
    """Get the most recently logged-in athlete (latest token)."""
    with get_session() as session:
        # Get most recent token
        stmt = select(OAuthToken).order_by(desc(OAuthToken.created_at)).limit(1)
        latest_token = session.execute(stmt).scalar_one_or_none()
        
        if not latest_token:
            return None
        
        # Get associated athlete
        athlete = session.get(Athlete, latest_token.athlete_id)
        return athlete


def _get_project_podium_athletes():
    """Get list of Project Podium athlete objects."""
    project_podium_names = [
        "Reese Vannerson", "Sullivan Middaugh", "Porter Middaugh",
        "Blake Bullard", "Blake Harris", "Carter Stuhlmacher",
        "Mathis Beaulieu", "Keller norland", "Jimena De La Pena", "Braxton Legg"
    ]
    
    all_athletes = list_athletes()
    project_podium_athletes = []
    
    for name in project_podium_names:
        athlete = next((a for a in all_athletes if a.name.lower() == name.lower()), None)
        if athlete:
            project_podium_athletes.append(athlete)
        else:
            print(f"⚠️  Warning: Athlete '{name}' not found in database")
    
    return project_podium_athletes


def _process_single_athlete(athlete_id: int, athlete_name: str, effective_date):
    """
    Process a single athlete: ingest data and retrieve compliance/recovery.
    
    Returns:
        dict with athlete summary data, or None if error
    """
    try:
        # Run ingestion
        result = ingest_recent(athlete_id=athlete_id, days=1)
        
        # Check for errors
        if result.get('error'):
            return {'error': result['error'], 'name': athlete_name}
        
        # Retrieve compliance data from database
        compliance_db = get_compliance_for_day(athlete_id, effective_date)
        compliance_records = compliance_db.get('records', []) if compliance_db else []
        
        # Extract recovery data
        recovery_alert = result.get('recovery_alert', {})
        
        # Calculate average compliance
        valid_scores = [c.get('overall_score') for c in compliance_records if c.get('overall_score') is not None]
        avg_compliance = sum(valid_scores) / len(valid_scores) if valid_scores else None
        
        return {
            'name': athlete_name,
            'recovery_triggered': recovery_alert.get('triggered', False),
            'recovery_reason': recovery_alert.get('reason', 'no_data'),
            'recovery_metrics': recovery_alert.get('metrics', {}),
            'compliance_records': compliance_records,
            'workouts_count': len(compliance_records),
            'avg_compliance': avg_compliance
        }
        
    except Exception as e:
        return {'error': str(e), 'name': athlete_name}


def _process_batch_athletes(athlete_list, effective_date):
    """
    Process multiple athletes and return summaries and errors.
    
    Returns:
        tuple: (athlete_summaries, errors)
    """
    athlete_summaries = []
    errors = []
    
    for idx, athlete in enumerate(athlete_list, 1):
        print(f"[{idx}/{len(athlete_list)}] Processing {athlete.name}...")
        
        summary = _process_single_athlete(athlete.id, athlete.name, effective_date)
        
        if summary.get('error'):
            errors.append(f"{athlete.name}: {summary['error']}")
            print(f"  ⚠️  Error: {summary['error']}")
        else:
            athlete_summaries.append(summary)
            print(f"  ✅ {athlete.name}: {summary['workouts_count']} workouts")
    
    return athlete_summaries, errors


def print_menu():
    """Display the main menu."""
    print("\n" + "=" * 70)
    print(" PODIUM DASHBOARD - TEST & AUTOMATION HELPER")
    print("=" * 70)
    print("\n🔐 OAUTH & AUTHENTICATION:")
    print("  1. Login with TrainingPeaks (OAuth)")
    print("  2. Check current token status")
    print("\n👥 ATHLETE MANAGEMENT:")
    print("  3. List all athletes")
    print("\n📊 DATA INGESTION:")
    print("  4. Ingest single day of data (metrics + workouts)")
    print("\n📧 AI EMAIL GENERATION:")
    print("  5. Generate coach summary email for single athlete")
    print("  6. Generate batch coach summary for Project Podium athletes (preview)")
    print("  8. Send live batch coach email via Resend (production)")
    print("\n📁 WORKOUT FILE DOWNLOADS:")
    print("  9. Download FIT files for single athlete (single day)")
    print(" 10. Download time series data for single athlete (single day)")
    print(" 11. Analyze FIT file (extract metrics, laps, HR zones, power)")
    print("\n🗑️  DATABASE CLEANUP:")
    print("  *. Delete today's data (workouts, metrics, compliance)")
    print("\n  0. Exit")
    print("=" * 70)


def oauth_login():
    """Option 1: Login with TrainingPeaks OAuth."""
    print("\n" + "=" * 70)
    print(" TRAININGPEAKS OAUTH LOGIN")
    print("=" * 70)
    
    # Check existing token
    existing_token = find_coach_token()
    if existing_token:
        print("\n⚠️  You already have a coach token!")
        print(f"Expires: {existing_token.expires_at}")
        print(f"Scope: {existing_token.scope}")
        
        reauth = input("\nRe-authorize anyway? (y/n): ").lower() == 'y'
        if not reauth:
            print("Keeping existing token.")
            return
    
    # Select role
    print("\nSelect authorization role:")
    print("  1. Athlete (athlete:profile, metrics:read, workouts)")
    print("  2. Coach (coach:athletes, metrics:read, workouts)")
    
    choice = input("\nEnter choice (1-2) [default: 2]: ").strip() or "2"
    
    if choice == "1":
        scopes = ["athlete:profile", "metrics:read", "workouts:read", "workouts:details", "workouts:wod"]
        role = "Athlete"
    else:
        scopes = ["coach:athletes", "metrics:read", "workouts:read", "workouts:details", "workouts:wod"]
        role = "Coach"
    
    print(f"\n✓ Selected: {role}")
    print(f"  Scopes: {' '.join(scopes)}")
    
    # Get authorization URL
    try:
        auth_url, state = get_authorization_url(scope=scopes)
        print(f"\n🔗 Opening browser to TrainingPeaks login...")
        print(f"\nIf browser doesn't open, visit this URL:")
        print(f"{auth_url}")
        
        # Open browser
        webbrowser.open(auth_url)
        
        print("\n" + "=" * 70)
        print("INSTRUCTIONS:")
        print("=" * 70)
        print("1. Complete the login in your browser")
        print("2. After approval, you'll be redirected to localhost:8501")
        print("3. Copy the FULL URL from your browser address bar")
        print("4. Paste it here")
        print("=" * 70)
        
        # Get redirect URL from user
        redirect_url = input("\nPaste the redirect URL here: ").strip()
        
        if not redirect_url:
            print("❌ No URL provided. Cancelled.")
            return
        
        # Extract code from URL
        parsed = urllib.parse.urlparse(redirect_url)
        params = urllib.parse.parse_qs(parsed.query)
        
        code = params.get('code', [None])[0]
        returned_state = params.get('state', [None])[0]
        
        if not code:
            print("❌ No authorization code found in URL.")
            print("Make sure you copied the complete URL after being redirected.")
            return
        
        print(f"\n✓ Authorization code extracted: {code[:20]}...")
        
        # Validate state
        if state and returned_state and state != returned_state:
            print("⚠️  OAuth state mismatch warning (proceeding anyway)")
        
        # Exchange code for token
        print("\n📡 Exchanging code for access token...")
        token = fetch_token(code, scope=None)
        
        print("✓ Token received successfully!")
        
        # Fetch profile to get athlete info
        print("\n🔍 Fetching athlete profile...")
        import requests
        headers = {
            "Authorization": f"Bearer {token['access_token']}", 
            "Accept": "application/json"
        }
        profile_url = f"{settings.tp_api_base}/v1/athlete/profile"
        
        athlete_id = None
        try:
            resp = requests.get(profile_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                profile = resp.json()
                
                # DEBUG: Print the full profile to see what fields are available
                print(f"\n🔍 DEBUG - Profile response fields: {list(profile.keys())}")
                print(f"🔍 DEBUG - Full profile: {profile}")
                
                # Try multiple field names for athlete ID (case-sensitive!)
                tp_athlete_id = (profile.get('Id') or profile.get('id') or 
                                profile.get('athleteId') or profile.get('AthleteId') or
                                profile.get('userId') or profile.get('UserId') or 
                                profile.get('user', {}).get('id'))
                
                # Build athlete name from FirstName/LastName or fall back to name field
                first_name = profile.get('FirstName') or profile.get('firstName', '')
                last_name = profile.get('LastName') or profile.get('lastName', '')
                if first_name or last_name:
                    athlete_name = f"{first_name} {last_name}".strip()
                else:
                    athlete_name = (profile.get('name') or profile.get('Name') or 
                                  profile.get('userName') or profile.get('UserName') or
                                  profile.get('user', {}).get('name', 'Unknown Athlete'))
                
                athlete_email = (profile.get('Email') or profile.get('email') or 
                               profile.get('user', {}).get('email'))
                
                print(f"✓ Profile verified: {athlete_name} (TP ID: {tp_athlete_id})")
                
                # Find or create athlete record with TP data
                from app.data.db import get_session
                from app.models.tables import Athlete
                from sqlalchemy import select
                
                with get_session() as session:
                    if tp_athlete_id:
                        # Try to find existing athlete by TP ID
                        stmt = select(Athlete).where(Athlete.tp_athlete_id == tp_athlete_id)
                        db_athlete = session.execute(stmt).scalar_one_or_none()
                        
                        if db_athlete:
                            # Update existing athlete
                            print(f"✓ Found existing athlete record (ID: {db_athlete.id})")
                            db_athlete.name = athlete_name
                            db_athlete.email = athlete_email or db_athlete.email
                            athlete_id = db_athlete.id
                        else:
                            # Create new athlete
                            print(f"✓ Creating new athlete record...")
                            new_athlete = Athlete(
                                external_id=f"tp_{tp_athlete_id}",
                                tp_athlete_id=tp_athlete_id,
                                name=athlete_name,
                                email=athlete_email
                            )
                            session.add(new_athlete)
                            session.flush()  # Get the ID
                            athlete_id = new_athlete.id
                            print(f"✓ Created athlete record (ID: {athlete_id})")
                    else:
                        # No TP ID in profile, use demo athlete
                        print("⚠️  No athlete ID in profile, using demo athlete...")
                        demo = get_or_create_demo_athlete()
                        athlete_id = demo.id
                    
                    session.commit()
            else:
                print(f"⚠️  Profile fetch returned status {resp.status_code}")
                print(f"Response: {resp.text[:200]}")
                print("Using demo athlete as fallback...")
                demo = get_or_create_demo_athlete()
                athlete_id = demo.id
        except Exception as e:
            print(f"⚠️  Profile verification failed: {e}")
            import traceback
            traceback.print_exc()
            print("Using demo athlete as fallback...")
            demo = get_or_create_demo_athlete()
            athlete_id = demo.id
        
        if not athlete_id:
            demo = get_or_create_demo_athlete()
            athlete_id = demo.id
        
        # Re-fetch athlete from database for display (avoids detached instance)
        from app.data.db import get_session
        from app.models.tables import Athlete
        with get_session() as session:
            athlete = session.get(Athlete, athlete_id)
            athlete_name = athlete.name
            athlete_display_id = athlete.id
        
        print(f"\n✓ Using athlete: {athlete_name} (ID: {athlete_display_id})")
        
        # Store token
        store_token(athlete_id, token)
        print("✓ Token stored in database")
        
        print("\n" + "=" * 70)
        print("✓ OAUTH LOGIN COMPLETE")
        print("=" * 70)
        print("\nYou can now use other menu options to test the system!")
        print("=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error during OAuth flow: {e}")
        print("\nTroubleshooting:")
        print("- Verify TP_CLIENT_ID and TP_CLIENT_SECRET in .env")
        print("- Ensure TP_REDIRECT_URI matches exactly (http://localhost:8501/)")
        print("- Check that you're using TrainingPeaks Sandbox credentials")


def check_token_status():
    """Option 2: Check current token status."""
    print("\n" + "=" * 70)
    print(" TOKEN STATUS")
    print("=" * 70)
    
    # Check for coach token
    coach_token = find_coach_token()
    
    if coach_token:
        print("\n✓ Coach token found!")
        print(f"  Athlete ID: {coach_token.athlete_id}")
        print(f"  Expires: {coach_token.expires_at}")
        print(f"  Scope: {coach_token.scope}")
        print(f"  Has access token: {bool(coach_token.access_token)}")
        print(f"  Has refresh token: {bool(coach_token.refresh_token)}")
        
        # Check if expired
        now = datetime.now(timezone.utc)
        if coach_token.expires_at < now:
            print("\n⚠️  Token is EXPIRED")
            print("Use option 1 to re-authenticate or the token will be auto-refreshed on use")
        else:
            time_left = coach_token.expires_at - now
            hours = time_left.total_seconds() / 3600
            print(f"\n✓ Token is valid for {hours:.1f} more hours")
    else:
        print("\n❌ No coach token found")
        print("Use option 1 to login with TrainingPeaks")
    
    # List all tokens
    from app.data.db import get_session
    from app.models.tables import OAuthToken
    from sqlalchemy import select
    
    with get_session() as session:
        all_tokens = session.execute(select(OAuthToken)).scalars().all()
        
        if len(all_tokens) > 1 or (len(all_tokens) == 1 and not coach_token):
            print(f"\n📋 All tokens in database ({len(all_tokens)} total):")
            for token in all_tokens:
                has_coach = "coach:athletes" in (token.scope or "").lower()
                print(f"\n  Athlete ID: {token.athlete_id}")
                print(f"    Scope: {token.scope}")
                print(f"    Expires: {token.expires_at}")
                print(f"    Coach token: {'✓' if has_coach else '✗'}")
    
    print("\n" + "=" * 70 + "\n")


def list_all_athletes():
    """Option 3: List all athletes."""
    print("\n" + "=" * 70)
    print(" ATHLETES IN DATABASE")
    print("=" * 70)
    
    athletes = list_athletes()
    
    if not athletes:
        print("\n❌ No athletes found in database.")
        print("Athletes will be created automatically when you:")
        print("  - Login with OAuth (option 1)")
        print("  - Fetch coach roster")
        print("  - Run data ingestion")
    else:
        print(f"\nFound {len(athletes)} athlete(s):\n")
        for athlete in athletes:
            print(f"ID: {athlete.id}")
            print(f"  Name: {athlete.name}")
            print(f"  Email: {athlete.email or 'N/A'}")
            print(f"  External ID: {athlete.external_id}")
            print(f"  TP Athlete ID: {athlete.tp_athlete_id or 'N/A'}")
            print()
    
    print("=" * 70 + "\n")


def ingest_single_day():
    """Option 4: Ingest single day of data."""
    print("\n" + "=" * 70)
    print(" INGEST SINGLE DAY DATA")
    print("=" * 70)
    
    # Check for token
    if not find_coach_token():
        print("\n❌ No coach token found!")
        print("Please use option 1 to login with TrainingPeaks first.")
        return
    
    # Show date info
    effective_today = get_effective_today()
    actual_today = date.today()
    offset = settings.sandbox_current_day_offset
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Will ingest data for: {effective_today}")
    
    # Select athlete
    current_athlete = _get_current_athlete()
    athletes = list_athletes()
    
    if not athletes:
        print("\n❌ No athletes found in database.")
        print("Using demo athlete...")
        athlete = None
        athlete_id = None
    else:
        print(f"\n👥 Available athletes:")
        if current_athlete:
            print(f"  0. {current_athlete.name} (TP ID: {current_athlete.tp_athlete_id or 'N/A'}) [Currently logged in]")
        for athlete in athletes:
            print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id or 'N/A'})")
        
        choice = input("\nEnter athlete ID (0 for current, or press Enter for demo athlete): ").strip()
        if choice == '0' and current_athlete:
            athlete = current_athlete
            athlete_id = current_athlete.id
        elif choice:
            try:
                athlete_id = int(choice)
                athlete = next((a for a in athletes if a.id == athlete_id), None)
                if not athlete:
                    print(f"❌ Athlete ID {athlete_id} not found. Using demo athlete.")
                    athlete_id = None
                    athlete = None
            except ValueError:
                print("❌ Invalid input. Using demo athlete.")
                athlete_id = None
                athlete = None
        else:
            athlete_id = None
            athlete = None
    
    athlete_name = athlete.name if athlete else "Demo Athlete"
    print(f"\n✓ Selected: {athlete_name}")
    
    # Confirm
    confirm = input(f"\nIngest data for {effective_today}? (y/n): ").lower()
    if confirm != 'y':
        print("Cancelled.")
        return
    
    # Run ingestion
    print(f"\n🔄 Ingesting data from TrainingPeaks...")
    print("=" * 70)
    
    try:
        result = ingest_recent(days=1, athlete_id=athlete_id)
        
        # Display results
        print("\n✓ INGESTION COMPLETE")
        print("=" * 70)
        
        print(f"\n📊 Summary:")
        print(f"  Date range: {result['range']}")
        print(f"  TP Athlete ID: {result['tp_athlete_id']}")
        print(f"  Used coach token: {result['used_coach_token']}")
        
        print(f"\n💪 Workouts:")
        print(f"  Fetched: {result['workouts_fetched']}")
        print(f"  Inserted: {result['workouts_inserted']}")
        print(f"  Duplicates: {result['workout_duplicates']}")
        if result['sample_workout_ids']:
            print(f"  Sample IDs: {', '.join(result['sample_workout_ids'][:3])}")
        
        print(f"\n📈 Metrics:")
        print(f"  Fetched: {result['metrics_fetched']}")
        print(f"  Saved: {result['metrics_saved']}")
        if result['metrics_dates_saved']:
            print(f"  Dates saved: {', '.join(result['metrics_dates_saved'])}")
        
        # Workout compliance details
        if result.get('compliance_updates'):
            print(f"\n✅ Workout Compliance:")
            for comp in result['compliance_updates']:
                sport = comp.get('sport', 'Unknown')
                score = comp.get('overall_score')
                notes = comp.get('notes', 'All good')
                score_display = f"{score:.0f}" if score else "N/A"
                print(f"  {sport}: Score {score_display}/100 - {notes}")
        
        # Latest compliance summary
        if result.get('latest_compliance'):
            latest = result['latest_compliance']
            if latest.get('records'):
                print(f"\n📋 Latest Compliance Details:")
                for record in latest['records']:
                    print(f"  Sport: {record.get('sport')}")
                    print(f"  Date: {record.get('workout_date')}")
                    print(f"  Overall Score: {record.get('overall_score', 'N/A')}")
                    
                    if record.get('metrics'):
                        print(f"  Metrics:")
                        for metric in record['metrics']:
                            metric_name = metric.get('metric', 'unknown')
                            planned = metric.get('planned', 'N/A')
                            actual = metric.get('actual', 'N/A')
                            rating = metric.get('rating', 'N/A')
                            unit = metric.get('unit', '')
                            print(f"    - {metric_name}: Planned {planned}{unit} vs Actual {actual}{unit} ({rating})")
        
        # Recovery alert
        if result.get('recovery_alert'):
            alert = result['recovery_alert']
            print(f"\n🚨 Recovery Alert:")
            print(f"  Triggered: {alert.get('triggered')}")
            print(f"  Reason: {alert.get('reason')}")
            if alert.get('metrics'):
                for metric_name, metric_data in alert['metrics'].items():
                    breached = "🔴 BREACHED" if metric_data.get('breached') else "🟢 OK"
                    current = metric_data.get('current', 'N/A')
                    baseline = metric_data.get('baseline', 'N/A')
                    print(f"  {metric_name}: {current} (baseline: {baseline}) {breached}")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error during ingestion: {e}")
        import traceback
        traceback.print_exc()


def generate_coach_email():
    """Option 5: Generate AI-powered coach summary email for single athlete."""
    print("\n" + "=" * 70)
    print(" GENERATE COACH SUMMARY EMAIL")
    print("=" * 70)
    
    # Get effective date
    actual_today = date.today()
    effective_today = get_effective_today()
    offset_days = (actual_today - effective_today).days
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset_days} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Will generate email for: {effective_today}")
    
    # Select athlete
    current_athlete = _get_current_athlete()
    athletes = list_athletes()
    if not athletes:
        print("\n❌ No athletes found!")
        return
    
    print(f"\n👥 Available athletes:")
    if current_athlete:
        print(f"  0. {current_athlete.name} (TP ID: {current_athlete.tp_athlete_id}) [Currently logged in]")
    for athlete in athletes:
        print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id})")
    
    athlete_input = input("\nEnter athlete ID (0 for current, or press Enter for demo athlete): ").strip()
    
    if athlete_input == '0' and current_athlete:
        athlete = current_athlete
    elif not athlete_input:
        athlete = get_or_create_demo_athlete()
    else:
        athlete = next((a for a in athletes if str(a.id) == athlete_input), None)
        if not athlete:
            print(f"❌ Athlete with ID {athlete_input} not found.")
            return
    
    print(f"\n✓ Selected: {athlete.name}")
    
    # Confirm ingestion
    confirm = input(f"\nIngest data for {effective_today}? (y/n): ").lower() == 'y'
    if not confirm:
        print("Cancelled.")
        return
    
    print("\n🔄 Processing athlete...")
    print("=" * 70 + "\n")
    
    try:
        # Get coach token
        coach_token = find_coach_token()
        if not coach_token:
            print("❌ No coach token found. Please run option 1 to login first.")
            return
        
        # Process athlete using helper function
        summary = _process_single_athlete(athlete.id, athlete.name, effective_today)
        
        if summary.get('error'):
            print(f"❌ Error: {summary['error']}")
            return
        
        print("\n✓ PROCESSING COMPLETE")
        print("=" * 70)
        
        # Prepare recovery data for LLM
        recovery_data = {
            'triggered': summary['recovery_triggered'],
            'reason': summary['recovery_reason'],
            'metrics': summary['recovery_metrics']
        }
        
        # Prepare compliance data for LLM
        compliance_data = {
            'records': summary['compliance_records']
        }
        
        # Generate email with LLM
        print("\n🤖 Generating coach summary email with AI...")
        print("=" * 70 + "\n")
        
        email_body = llm_client.generate_daily_summary(
            athlete_name=athlete.name,
            date_str=str(effective_today),
            recovery_data=recovery_data,
            compliance_data=compliance_data
        )
        
        # Display generated email
        print("\n📧 GENERATED EMAIL:")
        print("=" * 70)
        print(email_body)
        print("=" * 70)
        
        # Summary stats
        print("\n📊 Email Generation Summary:")
        print(f"  Athlete: {athlete.name}")
        print(f"  Date: {effective_today}")
        print(f"  Recovery Alert: {'Yes' if recovery_data['triggered'] else 'No'}")
        print(f"  Workouts Analyzed: {summary['workouts_count']}")
        if summary['avg_compliance'] is not None:
            print(f"  Average Compliance Score: {summary['avg_compliance']:.1f}/100")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error during email generation: {e}")
        import traceback
        traceback.print_exc()


def generate_batch_coach_email():
    """Option 6: Generate batch coach summary email for all Project Podium athletes (preview)."""
    print("\n" + "=" * 70)
    print(" BATCH COACH SUMMARY - PROJECT PODIUM ATHLETES (PREVIEW)")
    print("=" * 70)
    
    # Get effective date
    actual_today = date.today()
    effective_today = get_effective_today()
    offset_days = (actual_today - effective_today).days
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset_days} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Will generate summary for: {effective_today}")
    
    # Get Project Podium athletes using helper
    project_podium_athletes = _get_project_podium_athletes()
    
    print(f"\n👥 Found {len(project_podium_athletes)} Project Podium athletes:")
    for athlete in project_podium_athletes:
        print(f"  - {athlete.name} (ID: {athlete.id})")
    
    if not project_podium_athletes:
        print("\n❌ No Project Podium athletes found!")
        return
    
    # Confirm processing
    confirm = input(f"\nProcess all {len(project_podium_athletes)} athletes for {effective_today}? (y/n): ").lower() == 'y'
    if not confirm:
        print("Cancelled.")
        return
    
    # Get coach token
    coach_token = find_coach_token()
    if not coach_token:
        print("❌ No coach token found. Please run option 1 to login first.")
        return
    
    print("\n🔄 Processing athletes...")
    print("=" * 70 + "\n")
    
    # Process all athletes using helper function
    athlete_summaries, errors = _process_batch_athletes(project_podium_athletes, effective_today)
    
    print("\n" + "=" * 70)
    print("✓ DATA COLLECTION COMPLETE")
    print("=" * 70)
    
    # Generate combined email with LLM
    print("\n🤖 Generating batch coach summary email with AI...")
    print("=" * 70 + "\n")
    
    try:
        email_body = llm_client.generate_batch_coach_summary(
            date_str=str(effective_today),
            athlete_summaries=athlete_summaries,
            errors=errors
        )
        
        # Display generated email
        print("\n📧 GENERATED BATCH EMAIL:")
        print("=" * 70)
        print(email_body)
        print("=" * 70)
        
        # Summary stats
        print("\n📊 Batch Email Generation Summary:")
        print(f"  Date: {effective_today}")
        print(f"  Athletes Processed: {len(athlete_summaries)}")
        print(f"  Errors: {len(errors)}")
        print(f"  Athletes with Recovery Alerts: {sum(1 for s in athlete_summaries if s['recovery_triggered'])}")
        total_workouts = sum(s['workouts_count'] for s in athlete_summaries)
        print(f"  Total Workouts Analyzed: {total_workouts}")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error generating batch email: {e}")
        import traceback
        traceback.print_exc()


def delete_todays_data():
    """Option 7: Delete all workout, metric, and compliance data for today."""
    print("\n" + "=" * 70)
    print(" DELETE TODAY'S DATA")
    print("=" * 70)
    
    try:
        # Get effective today (accounting for offset)
        effective_today = get_effective_today()
        print(f"\n📅 Target Date: {effective_today.isoformat()}")
        print(f"⚠️  This will delete ALL workouts, metrics, and compliance records for this date.")
        
        # Confirm deletion
        confirm = input("\nAre you sure you want to delete today's data? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("\n❌ Deletion cancelled.")
            return
        
        with get_session() as session:
            # Count records before deletion
            workout_count = session.query(Workout).filter(Workout.date == effective_today).count()
            metric_count = session.query(DailyMetric).filter(DailyMetric.date == effective_today).count()
            compliance_count = session.query(WorkoutCompliance).filter(WorkoutCompliance.workout_date == effective_today).count()
            
            print(f"\n📊 Found:")
            print(f"  - {workout_count} workouts")
            print(f"  - {metric_count} daily metrics")
            print(f"  - {compliance_count} compliance records")
            
            if workout_count == 0 and metric_count == 0 and compliance_count == 0:
                print("\n✅ No data found for today. Nothing to delete.")
                return
            
            # Delete compliance records first (foreign key dependency)
            compliance_deleted = session.execute(
                delete(WorkoutCompliance).where(WorkoutCompliance.workout_date == effective_today)
            )
            
            # Delete workouts
            workout_deleted = session.execute(
                delete(Workout).where(Workout.date == effective_today)
            )
            
            # Delete metrics
            metric_deleted = session.execute(
                delete(DailyMetric).where(DailyMetric.date == effective_today)
            )
            
            session.commit()
            
            print(f"\n✅ Successfully deleted:")
            print(f"  - {compliance_deleted.rowcount} compliance records")
            print(f"  - {workout_deleted.rowcount} workouts")
            print(f"  - {metric_deleted.rowcount} daily metrics")
            
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error deleting data: {e}")
        import traceback
        traceback.print_exc()


def send_live_coach_email():
    """Option 8: Send live batch coach email via Resend (production mode)."""
    print("\n" + "=" * 70)
    print(" SEND LIVE BATCH COACH EMAIL (PRODUCTION)")
    print("=" * 70)
    
    try:
        # Date setup
        actual_today = date.today()
        effective_today = get_effective_today()
        offset_days = (actual_today - effective_today).days
        
        print(f"\n📅 Date Information:")
        print(f"  Actual today: {actual_today}")
        print(f"  Sandbox offset: {offset_days} days")
        print(f"  Effective 'today': {effective_today}")
        print(f"  Email subject date: {effective_today}")
        
        print(f"\n📧 Recipient: {settings.head_coach_email}")
        print(f"📤 Delivery method: Resend API (LIVE EMAIL)")
        
        # Get Project Podium athletes using helper
        project_podium_athletes = _get_project_podium_athletes()
        
        print(f"\n👥 Found {len(project_podium_athletes)} Project Podium athletes")
        
        if not project_podium_athletes:
            print("\n❌ No Project Podium athletes found!")
            return
        
        # Confirm sending
        confirm = input(f"\n⚠️  This will send a REAL EMAIL to {settings.head_coach_email}. Continue? (yes/no): ").strip().lower()
        if confirm != 'yes':
            print("\n❌ Email sending cancelled.")
            return
        
        # Get coach token
        coach_token = find_coach_token()
        if not coach_token:
            print("❌ No coach token found. Please run option 1 to login first.")
            return
        
        print("\n🔄 Processing athletes...")
        print("=" * 70 + "\n")
        
        # Process all athletes using helper function
        athlete_summaries, errors = _process_batch_athletes(project_podium_athletes, effective_today)
        
        # Generate AI summary email
        print("\n🤖 Generating AI summary email...")
        email_body = llm_client.generate_batch_coach_summary(
            date_str=effective_today.isoformat(),
            athlete_summaries=athlete_summaries,
            errors=errors
        )
        
        if not email_body:
            print("\n❌ Failed to generate email content!")
            return
        
        # Send email via Resend
        print("\n📧 Sending email via Resend...")
        subject = f"Project Podium Daily Summary - {effective_today.isoformat()}"
        
        send_result = email_client.send_text_email(
            to_email=settings.head_coach_email,
            subject=subject,
            body=email_body
        )
        
        print("\n" + "=" * 70)
        print(" EMAIL SEND RESULT")
        print("=" * 70)
        print(f"\nStatus: {send_result.get('status', 'unknown')}")
        
        if send_result.get('status') == 'sent':
            print(f"✅ Email successfully sent!")
            print(f"Email ID: {send_result.get('email_id', 'N/A')}")
            print(f"Provider: {send_result.get('provider', 'N/A')}")
            print(f"To: {settings.head_coach_email}")
        elif send_result.get('status') == 'logged':
            print(f"⚠️  Email logged (not sent): {send_result.get('reason', 'unknown')}")
        else:
            print(f"❌ Email failed: {send_result.get('error', 'unknown')}")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error sending email: {e}")
        import traceback
        traceback.print_exc()


def download_time_series_data():
    """Option 10: Download time series data for a single athlete on a single day."""
    print("\n" + "=" * 70)
    print(" DOWNLOAD TIME SERIES DATA")
    print("=" * 70)
    
    # Check for any token (coach or athlete)
    if not _has_any_token():
        print("\n❌ No TrainingPeaks login found!")
        print("Please use option 1 to login with TrainingPeaks first.")
        return
    
    # Show date info
    effective_today = get_effective_today()
    actual_today = date.today()
    offset = settings.sandbox_current_day_offset
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Will download time series for: {effective_today}")
    
    # Select athlete
    current_athlete = _get_current_athlete()
    athletes = list_athletes()
    if not athletes:
        print("\n❌ No athletes found in database.")
        print("Using demo athlete...")
        athlete = None
        athlete_id = None
    else:
        print(f"\n👥 Available athletes:")
        if current_athlete:
            print(f"  0. {current_athlete.name} (TP ID: {current_athlete.tp_athlete_id}) [Currently logged in]")
        for athlete in athletes:
            print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id})")
        
        choice = input("\nEnter athlete ID (0 for current, or press Enter for demo athlete): ").strip()
        if choice == '0' and current_athlete:
            athlete = current_athlete
            athlete_id = current_athlete.id
        elif choice:
            try:
                athlete_id = int(choice)
                athlete = next((a for a in athletes if a.id == athlete_id), None)
                if not athlete:
                    print(f"❌ Athlete ID {athlete_id} not found!")
                    return
            except ValueError:
                print("❌ Invalid athlete ID!")
                return
        else:
            athlete = get_or_create_demo_athlete()
            athlete_id = athlete.id
    
    athlete_name = athlete.name if athlete else "Demo Athlete"
    print(f"\n✓ Selected: {athlete_name}")
    
    # Get API client
    from app.services.tp_api import get_api
    api = get_api(athlete_id)
    
    # First, fetch the day's workouts to get workout IDs
    print(f"\n🔍 Fetching workouts for {effective_today}...")
    try:
        tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
        workouts = api.fetch_workouts(effective_today, effective_today, tp_athlete_id=tp_athlete_id)
        
        if not workouts:
            print(f"\n❌ No workouts found for {effective_today}")
            return
        
        print(f"\n✓ Found {len(workouts)} workout(s)")
        
        # Display workouts
        print("\n" + "=" * 70)
        print("AVAILABLE WORKOUTS:")
        print("=" * 70)
        for idx, w in enumerate(workouts, 1):
            wid = w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
            sport = w.get('WorkoutType') or w.get('sportType') or 'Unknown'
            title = w.get('Title') or w.get('title') or 'Untitled'
            completed = w.get('Completed', False)
            print(f"\n{idx}. Workout ID: {wid}")
            print(f"   Sport: {sport}")
            print(f"   Title: {title}")
            print(f"   Completed: {'Yes' if completed else 'No'}")
        
        # Create output directory
        import os
        output_dir = os.path.join(os.getcwd(), "workout_timeseries")
        os.makedirs(output_dir, exist_ok=True)
        print(f"\n📁 Output directory: {output_dir}")
        
        # Download time series data
        print("\n" + "=" * 70)
        print("DOWNLOADING TIME SERIES DATA:")
        print("=" * 70)
        
        successful_downloads = []
        failed_downloads = []
        
        for idx, w in enumerate(workouts, 1):
            wid = w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
            if not wid:
                print(f"\n[{idx}/{len(workouts)}] ⚠️  Skipping workout - no ID found")
                continue
            
            sport = w.get('WorkoutType') or w.get('sportType') or 'Unknown'
            completed = w.get('Completed', False)
            
            # Skip uncompleted workouts (they won't have time series data)
            if not completed:
                print(f"\n[{idx}/{len(workouts)}] ⚠️  Skipping workout {wid} - not completed yet")
                continue
            
            print(f"\n[{idx}/{len(workouts)}] Downloading time series for workout {wid} ({sport})...")
            
            try:
                data = api.fetch_workout_time_series(str(wid), tp_athlete_id=tp_athlete_id)
                
                # Save as JSON
                import json
                filename = f"timeseries_{wid}_{effective_today.isoformat()}.json"
                filepath = os.path.join(output_dir, filename)
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                
                file_size = len(json.dumps(data))
                
                # Analyze the data structure
                channels = []
                data_points = 0
                has_lap_stats = False
                has_swim_stats = False
                
                if 'WorkoutChannels' in data and data['WorkoutChannels']:
                    channels = data['WorkoutChannels'].get('Channels', [])
                    data_points = len(data['WorkoutChannels'].get('Data', []))
                
                if 'LapStats' in data and data['LapStats']:
                    has_lap_stats = True
                
                if 'SwimStats' in data and data['SwimStats']:
                    has_swim_stats = True
                
                print(f"  ✓ Saved: {filepath}")
                print(f"  ✓ Size: {file_size:,} bytes")
                print(f"  ✓ Channels: {len(channels)} ({', '.join(channels[:5])}{'...' if len(channels) > 5 else ''})")
                print(f"  ✓ Data Points: {data_points:,}")
                if has_lap_stats:
                    print(f"  ✓ Laps: {len(data['LapStats'])}")
                if has_swim_stats:
                    print(f"  ✓ Swim Data: Yes")
                
                # Extract key stats if available
                if 'WorkoutStats' in data and data['WorkoutStats']:
                    stats = data['WorkoutStats']
                    print(f"  ✓ Stats: TSS={stats.get('Tss', 'N/A')}, IF={stats.get('IF', 'N/A')}, "
                          f"Avg HR={stats.get('HeartRateAverage', 'N/A')}, "
                          f"Avg Power={stats.get('PowerAverage', 'N/A')}")
                
                successful_downloads.append({
                    'workout_id': wid,
                    'filename': filename,
                    'filepath': filepath,
                    'size': file_size,
                    'sport': sport,
                    'channels': len(channels),
                    'data_points': data_points,
                    'has_laps': has_lap_stats,
                    'has_swim': has_swim_stats
                })
                
            except RuntimeError as e:
                error_msg = str(e)
                print(f"  ❌ Error: {error_msg}")
                failed_downloads.append({
                    'workout_id': wid,
                    'error': error_msg,
                    'sport': sport
                })
            except Exception as e:
                print(f"  ❌ Unexpected error: {e}")
                import traceback
                traceback.print_exc()
                failed_downloads.append({
                    'workout_id': wid,
                    'error': str(e),
                    'sport': sport
                })
        
        # Summary
        print("\n" + "=" * 70)
        print("DOWNLOAD SUMMARY")
        print("=" * 70)
        print(f"\n✓ Successful: {len(successful_downloads)}")
        print(f"❌ Failed: {len(failed_downloads)}")
        
        if successful_downloads:
            print("\n📁 Downloaded time series files:")
            total_data_points = 0
            for dl in successful_downloads:
                print(f"\n  - {dl['filename']}")
                print(f"    Sport: {dl['sport']}")
                print(f"    Size: {dl['size']:,} bytes")
                print(f"    Channels: {dl['channels']}")
                print(f"    Data Points: {dl['data_points']:,}")
                if dl['has_laps']:
                    print(f"    Has Lap Data: Yes")
                if dl['has_swim']:
                    print(f"    Has Swim Data: Yes")
                total_data_points += dl['data_points']
            
            print(f"\n  📊 Total data points across all workouts: {total_data_points:,}")
        
        if failed_downloads:
            print("\n⚠️  Failed downloads:")
            has_connection_error = False
            for fail in failed_downloads:
                print(f"  - Workout {fail['workout_id']} ({fail['sport']}): {fail['error']}")
                if 'connection failure' in fail['error'].lower() or 'too large' in fail['error'].lower():
                    has_connection_error = True
            
            if has_connection_error:
                print("\n💡 TIP: For workouts with very large time series data:")
                print("   • Try Option 9 to download as FIT file instead")
                print("   • FIT files are compressed binary format and handle large datasets better")
                print("   • You can then use FIT file parsers to extract the time series data")
        
        print("\n💡 Use Case: Time series data includes second-by-second measurements")
        print("   for heart rate, power, cadence, GPS, elevation, and more.")
        print("   Perfect for detailed workout analysis when FIT files aren't available.")
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


def download_fit_files():
    """Option 9: Download FIT files for a single athlete on a single day."""
    print("\n" + "=" * 70)
    print(" DOWNLOAD FIT FILES")
    print("=" * 70)
    
    # Check for any token (coach or athlete)
    if not _has_any_token():
        print("\n❌ No TrainingPeaks login found!")
        print("Please use option 1 to login with TrainingPeaks first.")
        return
    
    # Show date info
    effective_today = get_effective_today()
    actual_today = date.today()
    offset = settings.sandbox_current_day_offset
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Will download workouts for: {effective_today}")
    
    # Select athlete
    current_athlete = _get_current_athlete()
    athletes = list_athletes()
    if not athletes:
        print("\n❌ No athletes found in database.")
        print("Using demo athlete...")
        athlete = None
        athlete_id = None
    else:
        print(f"\n👥 Available athletes:")
        if current_athlete:
            print(f"  0. {current_athlete.name} (TP ID: {current_athlete.tp_athlete_id}) [Currently logged in]")
        for athlete in athletes:
            print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id})")
        
        choice = input("\nEnter athlete ID (0 for current, or press Enter for demo athlete): ").strip()
        if choice == '0' and current_athlete:
            athlete = current_athlete
            athlete_id = current_athlete.id
        elif choice:
            try:
                athlete_id = int(choice)
                athlete = next((a for a in athletes if a.id == athlete_id), None)
                if not athlete:
                    print(f"❌ Athlete ID {athlete_id} not found!")
                    return
            except ValueError:
                print("❌ Invalid athlete ID!")
                return
        else:
            athlete = get_or_create_demo_athlete()
            athlete_id = athlete.id
    
    athlete_name = athlete.name if athlete else "Demo Athlete"
    print(f"\n✓ Selected: {athlete_name}")
    
    # Get API client
    from app.services.tp_api import get_api
    api = get_api(athlete_id)
    
    # First, fetch the day's workouts to get workout IDs
    print(f"\n🔍 Fetching workouts for {effective_today}...")
    try:
        tp_athlete_id = getattr(athlete, 'tp_athlete_id', None)
        workouts = api.fetch_workouts(effective_today, effective_today, tp_athlete_id=tp_athlete_id)
        
        if not workouts:
            print(f"\n❌ No workouts found for {effective_today}")
            return
        
        print(f"\n✓ Found {len(workouts)} workout(s)")
        
        # Display workouts
        print("\n" + "=" * 70)
        print("AVAILABLE WORKOUTS:")
        print("=" * 70)
        for idx, w in enumerate(workouts, 1):
            wid = w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
            sport = w.get('WorkoutType') or w.get('sportType') or 'Unknown'
            title = w.get('Title') or w.get('title') or 'Untitled'
            print(f"\n{idx}. Workout ID: {wid}")
            print(f"   Sport: {sport}")
            print(f"   Title: {title}")
        
        # Select file format
        print("\n" + "=" * 70)
        print("FILE FORMAT OPTIONS:")
        print("=" * 70)
        print("  1. FIT (binary format for Garmin/devices)")
        print("  2. JSON (structured workout data)")
        print("  3. MRC (Computrainer format)")
        print("  4. ERG (Ergometer format)")
        print("  5. ZWO (Zwift format)")
        
        format_choice = input("\nSelect format (1-5) [default: 1 for FIT]: ").strip() or "1"
        
        format_map = {
            "1": "fit",
            "2": "json",
            "3": "mrc",
            "4": "erg",
            "5": "zwo"
        }
        
        file_format = format_map.get(format_choice, "fit")
        print(f"\n✓ Selected format: {file_format.upper()}")
        
        # Create output directory
        import os
        output_dir = os.path.join(os.getcwd(), "workout_files")
        os.makedirs(output_dir, exist_ok=True)
        print(f"\n📁 Output directory: {output_dir}")
        
        # Download files
        print("\n" + "=" * 70)
        print("DOWNLOADING FILES:")
        print("=" * 70)
        
        successful_downloads = []
        failed_downloads = []
        
        for idx, w in enumerate(workouts, 1):
            wid = w.get('workoutId') or w.get('id') or w.get('Id') or w.get('WorkoutId')
            if not wid:
                print(f"\n[{idx}/{len(workouts)}] ⚠️  Skipping workout - no ID found")
                continue
            
            sport = w.get('WorkoutType') or w.get('sportType') or 'Unknown'
            print(f"\n[{idx}/{len(workouts)}] Downloading workout {wid} ({sport})...")
            
            try:
                result = api.fetch_workout_file(str(wid), file_format, tp_athlete_id=tp_athlete_id)
                
                # Save file
                filename = result['filename']
                filepath = os.path.join(output_dir, filename)
                
                if file_format == 'json':
                    # Save JSON as text
                    import json
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(result['content'], f, indent=2)
                    file_size = len(json.dumps(result['content']))
                else:
                    # Save binary file
                    with open(filepath, 'wb') as f:
                        f.write(result['content'])
                    file_size = len(result['content'])
                
                print(f"  ✓ Saved: {filepath}")
                print(f"  ✓ Size: {file_size:,} bytes")
                
                successful_downloads.append({
                    'workout_id': wid,
                    'filename': filename,
                    'filepath': filepath,
                    'size': file_size,
                    'sport': sport
                })
                
            except RuntimeError as e:
                error_msg = str(e)
                print(f"  ❌ Error: {error_msg}")
                failed_downloads.append({
                    'workout_id': wid,
                    'error': error_msg,
                    'sport': sport
                })
            except Exception as e:
                print(f"  ❌ Unexpected error: {e}")
                failed_downloads.append({
                    'workout_id': wid,
                    'error': str(e),
                    'sport': sport
                })
        
        # Summary
        print("\n" + "=" * 70)
        print("DOWNLOAD SUMMARY")
        print("=" * 70)
        print(f"\n✓ Successful: {len(successful_downloads)}")
        print(f"❌ Failed: {len(failed_downloads)}")
        
        if successful_downloads:
            print("\n📁 Downloaded files:")
            for dl in successful_downloads:
                print(f"  - {dl['filename']} ({dl['size']:,} bytes) - {dl['sport']}")
        
        if failed_downloads:
            print("\n⚠️  Failed downloads:")
            for fail in failed_downloads:
                print(f"  - Workout {fail['workout_id']} ({fail['sport']}): {fail['error']}")
            print("\n💡 Note: Workouts without structure cannot be exported to FIT/structured formats.")
            print("   Only structured (planned) workouts support file export.")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


def analyze_fit_file():
    """Option 11: Analyze a FIT file and display comprehensive metrics."""
    print("\n" + "=" * 70)
    print(" ANALYZE FIT FILE")
    print("=" * 70)
    
    try:
        import os
        import json
        
        # Ask for file path
        print("\nEnter the path to the FIT file:")
        print("(e.g., C:\\Users\\johnk\\Downloads\\workout_3444886827.fit)")
        file_path = input("\nFile path: ").strip().strip('"')
        
        if not file_path:
            print("❌ No file path provided.")
            return
        
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return
        
        # Analysis options
        print("\n" + "=" * 70)
        print("Analysis Options:")
        print("  1. Quick summary (session + laps only)")
        print("  2. Full analysis (includes all time-series records)")
        print("  3. HR zone analysis")
        print("  4. Power curve analysis")
        print("  5. Export to JSON (full analysis)")
        
        choice = input("\nSelect analysis type (1-5) [default: 1]: ").strip() or "1"
        
        print("\n" + "=" * 70)
        print("Analyzing FIT file...")
        print("=" * 70 + "\n")
        
        analyzer = FitFileAnalyzer(file_path)
        
        if choice == "1":
            # Quick summary
            summary = analyzer.get_quick_summary()
            
            print("📊 WORKOUT SUMMARY")
            print("=" * 70)
            print(f"\n🏃 Sport: {summary.get('sport', 'N/A')}")
            
            if summary.get('duration_seconds'):
                minutes = int(summary['duration_seconds'] // 60)
                seconds = int(summary['duration_seconds'] % 60)
                print(f"⏱️  Duration: {minutes}:{seconds:02d}")
            
            if summary.get('distance_km'):
                print(f"📏 Distance: {summary['distance_km']} km ({summary.get('distance_miles', 'N/A')} mi)")
            
            if summary.get('avg_pace_per_km'):
                print(f"🏃 Avg Pace: {summary['avg_pace_per_km']} /km ({summary.get('avg_pace_per_mile', 'N/A')} /mi)")
            
            if summary.get('avg_heart_rate'):
                print(f"💓 Heart Rate: {summary['avg_heart_rate']} avg / {summary.get('max_heart_rate', 'N/A')} max bpm")
            
            if summary.get('avg_power'):
                print(f"⚡ Power: {summary['avg_power']} avg / {summary.get('max_power', 'N/A')} max watts")
            
            if summary.get('total_calories'):
                print(f"🔥 Calories: {summary['total_calories']}")
            
            print(f"\n📊 Laps: {summary.get('lap_count', 0)}")
            print(f"📈 Data Points: {summary.get('record_count', 0)}")
            
        elif choice == "2":
            # Full analysis
            analysis = analyzer.analyze(include_records=True)
            
            print("📊 FULL WORKOUT ANALYSIS")
            print("=" * 70)
            
            # Session summary
            session = analysis.session
            print(f"\n🏃 Sport: {session.sport} - {session.sub_sport or 'N/A'}")
            
            if session.start_time:
                print(f"📅 Date: {session.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            if session.total_timer_time:
                minutes = int(session.total_timer_time // 60)
                seconds = int(session.total_timer_time % 60)
                print(f"⏱️  Duration: {minutes}:{seconds:02d}")
            
            if session.total_distance:
                km = session.total_distance / 1000
                miles = session.total_distance / 1609.34
                print(f"📏 Distance: {km:.2f} km ({miles:.2f} mi)")
            
            if session.avg_heart_rate:
                print(f"💓 Heart Rate: {session.avg_heart_rate} avg / {session.max_heart_rate} max bpm")
            
            if session.avg_power:
                print(f"⚡ Power: {session.avg_power}W avg / {session.max_power}W max")
                if session.normalized_power:
                    print(f"   Normalized Power: {session.normalized_power}W")
            
            if session.total_calories:
                print(f"🔥 Calories: {session.total_calories}")
            
            if session.total_ascent:
                print(f"⛰️  Elevation: {session.total_ascent:.0f}m ascent / {session.total_descent:.0f}m descent")
            
            if session.training_stress_score:
                print(f"📈 TSS: {session.training_stress_score:.1f}")
            
            if session.intensity_factor:
                print(f"📊 IF: {session.intensity_factor:.3f}")
            
            # Laps
            print(f"\n📊 LAPS ({len(analysis.laps)})")
            print("-" * 70)
            for lap in analysis.laps:
                print(f"\nLap {lap.lap_number}:")
                if lap.total_timer_time:
                    minutes = int(lap.total_timer_time // 60)
                    seconds = int(lap.total_timer_time % 60)
                    print(f"  Time: {minutes}:{seconds:02d}")
                if lap.total_distance:
                    print(f"  Distance: {lap.total_distance / 1000:.2f} km")
                if lap.avg_heart_rate:
                    print(f"  HR: {lap.avg_heart_rate} avg / {lap.max_heart_rate} max")
                if lap.avg_power:
                    print(f"  Power: {lap.avg_power}W avg / {lap.max_power}W max")
            
            # Devices
            if analysis.devices:
                print(f"\n📱 DEVICES ({len(analysis.devices)})")
                print("-" * 70)
                for device in analysis.devices:
                    print(f"  {device.manufacturer} {device.product}")
                    if device.serial_number:
                        print(f"    Serial: {device.serial_number}")
            
            print(f"\n📈 Total Data Points: {len(analysis.records)}")
            
        elif choice == "3":
            # HR zone analysis
            max_hr_input = input("\nEnter your max heart rate [default: 190]: ").strip()
            max_hr = int(max_hr_input) if max_hr_input else 190
            
            zones = analyzer.extract_hr_zones(max_hr=max_hr)
            
            print("💓 HEART RATE ZONE ANALYSIS")
            print("=" * 70)
            print(f"\nMax HR: {max_hr} bpm\n")
            
            for zone_name, minutes in zones.items():
                if zone_name != 'total_minutes':
                    percentage = (minutes / zones['total_minutes'] * 100) if zones['total_minutes'] > 0 else 0
                    bar_length = int(percentage / 2)  # Scale to 50 chars max
                    bar = "█" * bar_length
                    print(f"{zone_name:20s} {minutes:6.1f} min ({percentage:5.1f}%) {bar}")
            
            print(f"\n{'Total':20s} {zones['total_minutes']:6.1f} min")
            
        elif choice == "4":
            # Power curve
            print("\n⚡ POWER CURVE ANALYSIS")
            print("=" * 70)
            
            durations_input = input("\nUse default durations? (y/n) [default: y]: ").strip().lower()
            
            if durations_input == 'n':
                print("Enter comma-separated durations in seconds (e.g., 5,10,60,300,1200):")
                custom_input = input("Durations: ").strip()
                durations = [int(d.strip()) for d in custom_input.split(',')]
                power_curve = analyzer.extract_power_curve(durations=durations)
            else:
                power_curve = analyzer.extract_power_curve()
            
            print("\nDuration     Max Avg Power")
            print("-" * 70)
            
            for duration, power in sorted(power_curve.items()):
                if duration < 60:
                    duration_str = f"{duration}s"
                elif duration < 3600:
                    duration_str = f"{duration // 60}min"
                else:
                    duration_str = f"{duration // 3600}h {(duration % 3600) // 60}min"
                
                print(f"{duration_str:12s} {power:6d}W")
            
        elif choice == "5":
            # Export to JSON
            analysis = analyzer.analyze(include_records=True)
            data = analysis.to_dict()
            
            # Generate output filename
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            output_file = os.path.join(os.path.dirname(file_path), f"{base_name}_analysis.json")
            
            with open(output_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            
            print(f"✅ Analysis exported to: {output_file}")
            print(f"   File size: {os.path.getsize(output_file):,} bytes")
        
        print("\n" + "=" * 70 + "\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


def main():
    """Main menu loop."""
    while True:
        print_menu()
        
        try:
            choice = input("\nEnter your choice (0-10, or *): ").strip()
            
            if choice == '0':
                print("\n👋 Goodbye!\n")
                sys.exit(0)
            elif choice == '1':
                oauth_login()
            elif choice == '2':
                check_token_status()
            elif choice == '3':
                list_all_athletes()
            elif choice == '4':
                ingest_single_day()
            elif choice == '5':
                generate_coach_email()
            elif choice == '6':
                generate_batch_coach_email()
            elif choice == '8':
                send_live_coach_email()
            elif choice == '9':
                download_fit_files()
            elif choice == '10':
                download_time_series_data()
            elif choice == '11':
                analyze_fit_file()
            elif choice == '*':
                delete_todays_data()
            else:
                print("\n❌ Invalid choice. Please enter 0-10 or *.")
                
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!\n")
            sys.exit(0)
        except Exception as e:
            print(f"\n❌ Error: {e}\n")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
