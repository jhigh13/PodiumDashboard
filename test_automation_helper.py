"""
Interactive helper script for testing automation features.

This script provides an easy-to-use interface for:
- Running live email tests
- Testing the daily scheduler
- Simulating time-based scenarios
- Manually triggering jobs
"""
import sys
from datetime import date, timedelta

from tests.test_recovery_alerts_live import (
    test_live_recovery_alert_email,
    test_live_with_existing_athlete,
    LiveTestHelper,
)
from tests.test_automation import (
    test_scheduler_daily_job_manual,
    test_time_simulation_scenario,
    test_scheduler_with_mock_time,
    AutomationTestHelper,
)
from app.scheduling.scheduler import daily_job, start_scheduler, scheduler
from app.services.recovery_alerts import evaluate_recovery_alert
from app.services.athletes import list_athletes, get_or_create_demo_athlete
from app.services.tokens import store_token, get_token, find_coach_token
from app.auth.oauth import get_authorization_url, fetch_token
from app.utils.settings import settings
import webbrowser


def print_menu():
    """Display the main menu."""
    print("\n" + "=" * 70)
    print(" PODIUM DASHBOARD - AUTOMATION TEST HELPER")
    print("=" * 70)
    print("\n� OAUTH & AUTHENTICATION:")
    print("  1. Login with TrainingPeaks (OAuth)")
    print("  2. Check current token status")
    print("\n�📧 LIVE EMAIL TESTS:")
    print("  3. Send live recovery alert email (creates test data)")
    print("  4. Send live email with existing athlete")
    print("\n⏰ SCHEDULER TESTS:")
    print("  5. Run daily job manually (right now)")
    print("  6. Test multi-day time simulation")
    print("  7. Test with mocked time progression")
    print("\n🔧 MANUAL OPERATIONS:")
    print("  8. List all athletes")
    print("  9. Check recovery alert for specific athlete")
    print(" 10. Create test data for specific date")
    print("\n⚙️  SCHEDULER CONTROL:")
    print(" 11. Start background scheduler")
    print(" 12. Check scheduler status")
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
        import urllib.parse
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
                
                # Update athlete record
                from app.data.db import get_session
                from app.models.tables import Athlete
                with get_session() as session:
                    db_athlete = session.get(Athlete, athlete.id)
                    if db_athlete:
                        db_athlete.tp_athlete_id = profile.get('athleteId') or profile.get('id')
                        db_athlete.name = profile.get('name') or db_athlete.name
                        db_athlete.email = profile.get('email') or db_athlete.email
                        session.commit()
                        print(f"✓ Athlete record updated")
            else:
                print(f"⚠️  Profile fetch returned status {resp.status_code}")
                print("Token may still work for other endpoints.")
        except Exception as e:
            print(f"⚠️  Profile verification failed: {e}")
            print("Token may still be valid.")
        
        print("\n" + "=" * 70)
        print("✓ OAUTH LOGIN COMPLETE")
        print("=" * 70)
        print("\nYou can now use option 5 to run the daily job!")
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
        from datetime import datetime, timezone
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


def send_live_test_email():
    """Option 1: Send a live test email."""
    print("\n⚠️  WARNING: This will send a REAL email to:", settings.head_coach_email)
    confirm = input("\nType 'yes' to continue: ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return
    
    test_live_recovery_alert_email()


def send_existing_athlete_email():
    """Option 2: Send email for existing athlete."""
    athletes = list_athletes()
    
    if not athletes:
        print("\n❌ No athletes found in database.")
        return
    
    print("\nAvailable athletes:")
    for athlete in athletes:
        print(f"  ID: {athlete.id} - {athlete.name}")
    
    try:
        athlete_id = int(input("\nEnter athlete ID: "))
        
        # Verify athlete exists
        athlete = next((a for a in athletes if a.id == athlete_id), None)
        if not athlete:
            print(f"❌ Athlete ID {athlete_id} not found.")
            return
        
        print(f"\n⚠️  This will send a REAL email to: {settings.head_coach_email}")
        print(f"Testing recovery alert for: {athlete.name}")
        confirm = input("\nType 'yes' to continue: ")
        if confirm.lower() != 'yes':
            print("Cancelled.")
            return
        
        # Use the existing athlete test
        helper = LiveTestHelper(athlete_id=athlete_id)
        
        try:
            print("\n" + "=" * 60)
            print(f"LIVE TEST WITH ATHLETE: {athlete.name} (ID: {athlete_id})")
            print("=" * 60)
            
            # Create baselines
            helper.create_baseline_metrics(
                athlete_id=athlete_id,
                hrv_mean=75.0,
                sleep_mean=7.5,
                rhr_mean=52.0,
            )
            print("✓ Created baseline metrics")
            
            # Create breached metrics
            test_date = date.today()
            helper.create_breached_metrics(
                athlete_id=athlete_id,
                test_date=test_date,
                hrv=63.0,
                sleep_hours=6.0,
                rhr=58.0,
            )
            print(f"✓ Created breached metrics for {test_date}")
            
            # Trigger alert
            print("\n📧 Sending recovery alert email...")
            result = evaluate_recovery_alert(
                athlete_id=athlete_id,
                check_date=test_date,
                threshold=0.05,
            )
            
            print(f"\n✓ Alert triggered: {result['triggered']}")
            print(f"  Reason: {result['reason']}")
            print(f"  Email status: {result.get('email_status', 'N/A')}")
            
            email_log = helper.verify_email_log(athlete_id, test_date)
            if email_log:
                print(f"✓ Email log recorded")
            
            print("\n" + "=" * 60)
            print("✓ TEST COMPLETE - Check your email!")
            print("=" * 60 + "\n")
            
        finally:
            print("Cleaning up test data...")
            helper.cleanup()
            print("✓ Cleanup complete\n")
            
    except ValueError:
        print("❌ Invalid athlete ID.")


def run_daily_job():
    """Option 5: Run the daily job manually for premium athletes only."""
    print("\n⚠️  This will run the daily job for premium athletes:")
    print("     - Reese Vannerson")
    print("     - Blake Bullard")
    print("     - Blake Harris")
    confirm = input("\nType 'yes' to continue: ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return
    
    test_scheduler_daily_job_manual()


def run_time_simulation():
    """Option 4: Run multi-day time simulation."""
    print("\n📅 This will simulate a multi-day scenario with test data")
    confirm = input("Type 'yes' to continue: ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return
    
    test_time_simulation_scenario()


def run_mock_time_test():
    """Option 5: Test with mocked time."""
    print("\n🕐 This will test with a mocked future date")
    confirm = input("Type 'yes' to continue: ")
    if confirm.lower() != 'yes':
        print("Cancelled.")
        return
    
    test_scheduler_with_mock_time()


def list_all_athletes():
    """Option 6: List all athletes."""
    athletes = list_athletes()
    
    if not athletes:
        print("\n❌ No athletes found in database.")
        return
    
    print(f"\n{'='*70}")
    print(f" ATHLETES IN DATABASE ({len(athletes)} total)")
    print(f"{'='*70}")
    
    for athlete in athletes:
        print(f"\nID: {athlete.id}")
        print(f"  Name: {athlete.name}")
        print(f"  Email: {athlete.email or 'N/A'}")
        print(f"  External ID: {athlete.external_id}")
        print(f"  TP Athlete ID: {athlete.tp_athlete_id or 'N/A'}")
    
    print(f"\n{'='*70}\n")


def check_specific_athlete_alert():
    """Option 7: Check recovery alert for a specific athlete."""
    athletes = list_athletes()
    
    if not athletes:
        print("\n❌ No athletes found in database.")
        return
    
    print("\nAvailable athletes:")
    for athlete in athletes:
        print(f"  ID: {athlete.id} - {athlete.name}")
    
    try:
        athlete_id = int(input("\nEnter athlete ID: "))
        
        # Verify athlete exists
        athlete = next((a for a in athletes if a.id == athlete_id), None)
        if not athlete:
            print(f"❌ Athlete ID {athlete_id} not found.")
            return
        
        # Allow custom date
        use_today = input("\nCheck for today? (y/n): ").lower() == 'y'
        if use_today:
            check_date = date.today()
        else:
            date_str = input("Enter date (YYYY-MM-DD): ")
            try:
                check_date = date.fromisoformat(date_str)
            except ValueError:
                print("❌ Invalid date format.")
                return
        
        print(f"\n🔍 Checking recovery alert for {athlete.name} on {check_date}...")
        result = evaluate_recovery_alert(
            athlete_id=athlete_id,
            check_date=check_date,
            threshold=0.05,
        )
        
        print("\nResults:")
        print(f"  Triggered: {result['triggered']}")
        print(f"  Reason: {result['reason']}")
        print(f"  Check Date: {result['check_date']}")
        
        if result['triggered']:
            print(f"  Email Status: {result.get('email_status', 'N/A')}")
        
        if result.get('metrics'):
            print("\n  Metrics:")
            for metric_name, metric_data in result['metrics'].items():
                print(f"    {metric_name}:")
                print(f"      Current: {metric_data.get('current')}")
                print(f"      Baseline: {metric_data.get('baseline')}")
                print(f"      Breached: {metric_data.get('breached')}")
        
        print()
        
    except ValueError:
        print("❌ Invalid athlete ID.")


def create_test_data():
    """Option 8: Create test data for a specific date."""
    print("\n📝 CREATE TEST DATA")
    print("=" * 70)
    
    helper = AutomationTestHelper()
    
    try:
        # Create or select athlete
        create_new = input("\nCreate new test athlete? (y/n): ").lower() == 'y'
        
        if create_new:
            name = input("Enter athlete name: ")
            athlete_id = helper.create_test_athlete(name)
            print(f"✓ Created athlete ID: {athlete_id}")
        else:
            athletes = list_athletes()
            if not athletes:
                print("❌ No athletes found. Creating test athlete...")
                athlete_id = helper.create_test_athlete()
                print(f"✓ Created athlete ID: {athlete_id}")
            else:
                print("\nAvailable athletes:")
                for athlete in athletes:
                    print(f"  ID: {athlete.id} - {athlete.name}")
                athlete_id = int(input("\nEnter athlete ID: "))
        
        # Date selection
        date_str = input("\nEnter date for metrics (YYYY-MM-DD) or 'today': ")
        if date_str.lower() == 'today':
            metric_date = date.today()
        else:
            try:
                metric_date = date.fromisoformat(date_str)
            except ValueError:
                print("❌ Invalid date format.")
                return
        
        # Baseline creation
        create_baseline = input("\nCreate baseline metrics? (y/n): ").lower() == 'y'
        if create_baseline:
            baseline_date = metric_date - timedelta(days=1)
            helper.inject_baseline_data(athlete_id, baseline_date)
            print(f"✓ Created baseline for {baseline_date}")
        
        # Metric type
        print("\nMetric type:")
        print("  1. Healthy (won't trigger alert)")
        print("  2. Breached (will trigger alert)")
        print("  3. Custom values")
        
        metric_type = input("Choose (1-3): ")
        
        if metric_type == '1':
            helper.inject_healthy_metrics(athlete_id, metric_date)
            print(f"✓ Created healthy metrics for {metric_date}")
        elif metric_type == '2':
            helper.inject_breached_metrics(athlete_id, metric_date)
            print(f"✓ Created breached metrics for {metric_date}")
        elif metric_type == '3':
            hrv = float(input("Enter HRV value: "))
            sleep = float(input("Enter sleep hours: "))
            rhr = float(input("Enter RHR value: "))
            helper.inject_metric_data(
                athlete_id=athlete_id,
                metric_date=metric_date,
                hrv=hrv,
                sleep_hours=sleep,
                rhr=rhr,
            )
            print(f"✓ Created custom metrics for {metric_date}")
        
        print("\n✓ Test data created successfully!")
        print("\nNote: Data will NOT be automatically cleaned up.")
        print("Use database tools to remove if needed.\n")
        
    except (ValueError, KeyboardInterrupt) as e:
        print(f"\n❌ Error: {e}")


def start_background_scheduler():
    """Option 9: Start the background scheduler."""
    print("\n⚙️  Starting background scheduler...")
    print(f"Scheduled time: {settings.daily_job_time}")
    
    try:
        start_scheduler()
        print("✓ Scheduler started successfully!")
        print("\nThe scheduler is now running in the background.")
        print(f"Daily job will run at {settings.daily_job_time} (America/Denver timezone)")
        print("\nNote: Keep this script running for the scheduler to work.")
        print("Press Ctrl+C to stop.\n")
        
        # Keep running
        import time
        while True:
            time.sleep(60)
            
    except KeyboardInterrupt:
        print("\n\n⏹️  Stopping scheduler...")
        if scheduler.running:
            scheduler.shutdown()
        print("✓ Scheduler stopped.\n")


def check_scheduler_status():
    """Option 10: Check scheduler status."""
    print("\n⚙️  SCHEDULER STATUS")
    print("=" * 70)
    print(f"Running: {scheduler.running}")
    print(f"Scheduled time: {settings.daily_job_time} (America/Denver)")
    
    if scheduler.running:
        jobs = scheduler.get_jobs()
        print(f"\nJobs: {len(jobs)}")
        for job in jobs:
            print(f"  - {job.id}: next run = {job.next_run_time}")
    else:
        print("\n❌ Scheduler is not running.")
        print("Use option 9 to start it.")
    
    print("=" * 70 + "\n")


def main():
    """Main menu loop."""
    while True:
        print_menu()
        
        try:
            choice = input("\nEnter your choice (0-12): ").strip()
            
            if choice == '0':
                print("\n👋 Goodbye!\n")
                sys.exit(0)
            elif choice == '1':
                oauth_login()
            elif choice == '2':
                check_token_status()
            elif choice == '3':
                send_live_test_email()
            elif choice == '4':
                send_existing_athlete_email()
            elif choice == '5':
                run_daily_job()
            elif choice == '6':
                run_time_simulation()
            elif choice == '7':
                run_mock_time_test()
            elif choice == '8':
                list_all_athletes()
            elif choice == '9':
                check_specific_athlete_alert()
            elif choice == '10':
                create_test_data()
            elif choice == '11':
                start_background_scheduler()
            elif choice == '12':
                check_scheduler_status()
            else:
                print("\n❌ Invalid choice. Please enter 0-12.")
                
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!\n")
            sys.exit(0)
        except Exception as e:
            print(f"\n❌ Error: {e}\n")


if __name__ == "__main__":
    main()
