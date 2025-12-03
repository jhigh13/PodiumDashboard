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
from app.utils.settings import settings
from app.utils.dates import get_effective_today
from app.data.db import get_session
from app.models.tables import Workout, DailyMetric, WorkoutCompliance
from sqlalchemy import delete


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

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
        scopes = ["athlete:profile", "metrics:read", "workouts:read", "workouts:details"]
        role = "Athlete"
    else:
        scopes = ["coach:athletes", "metrics:read", "workouts:read", "workouts:details"]
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
        
        # Get or create athlete
        athlete = get_or_create_demo_athlete()
        print(f"✓ Using athlete: {athlete.name} (ID: {athlete.id})")
        
        # Store token
        store_token(athlete.id, token)
        print("✓ Token stored in database")
        
        # Verify token works by fetching profile
        print("\n🔍 Verifying token by fetching profile...")
        import requests
        headers = {
            "Authorization": f"Bearer {token['access_token']}", 
            "Accept": "application/json"
        }
        profile_url = f"{settings.tp_api_base}/v1/athlete/profile"
        
        try:
            resp = requests.get(profile_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                profile = resp.json()
                print(f"✓ Profile verified: {profile.get('name', 'Unknown')}")
                
                # Update athlete record with TP data
                from app.data.db import get_session
                from app.models.tables import Athlete
                with get_session() as session:
                    db_athlete = session.get(Athlete, athlete.id)
                    if db_athlete:
                        db_athlete.tp_athlete_id = profile.get('athleteId') or profile.get('id')
                        db_athlete.name = profile.get('name') or db_athlete.name
                        db_athlete.email = profile.get('email') or db_athlete.email
                        session.commit()
                        print(f"✓ Athlete record updated with TP data")
            else:
                print(f"⚠️  Profile fetch returned status {resp.status_code}")
                print("Token may still work for other endpoints.")
        except Exception as e:
            print(f"⚠️  Profile verification failed: {e}")
            print("Token may still be valid.")
        
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
    athletes = list_athletes()
    if not athletes:
        print("\n❌ No athletes found in database.")
        print("Using demo athlete...")
        athlete = None
        athlete_id = None
    else:
        print(f"\n👥 Available athletes:")
        for athlete in athletes:
            print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id or 'N/A'})")
        
        choice = input("\nEnter athlete ID (or press Enter for demo athlete): ").strip()
        if choice:
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
    athletes = list_athletes()
    if not athletes:
        print("\n❌ No athletes found!")
        return
    
    print(f"\n👥 Available athletes:")
    for athlete in athletes:
        print(f"  {athlete.id}. {athlete.name} (TP ID: {athlete.tp_athlete_id})")
    
    athlete_input = input("\nEnter athlete ID (or press Enter for demo athlete): ").strip()
    
    if not athlete_input:
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


def main():
    """Main menu loop."""
    while True:
        print_menu()
        
        try:
            choice = input("\nEnter your choice (0-8, or *): ").strip()
            
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
            elif choice == '*':
                delete_todays_data()
            else:
                print("\n❌ Invalid choice. Please enter 0-8 or *.")
                
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!\n")
            sys.exit(0)
        except Exception as e:
            print(f"\n❌ Error: {e}\n")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
