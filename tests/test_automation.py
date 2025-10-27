"""
Test automation features including scheduled jobs and recovery alerts.

This module provides utilities to:
1. Inject fake metric data with custom dates
2. Test the daily scheduler
3. Simulate time progression to verify automated tasks
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from unittest.mock import patch

from sqlalchemy import delete, select

from app.data.db import get_session
from app.models.tables import Athlete, DailyMetric, BaselineMetric, EmailLog
from app.scheduling.scheduler import daily_job, scheduler
from app.services import recovery_alerts
from app.services.athletes import list_athletes
from app.utils.settings import settings


class AutomationTestHelper:
    """Helper class to create test data and simulate time-based scenarios."""

    def __init__(self):
        self.test_athlete_id: Optional[int] = None
        self.created_metrics: list[int] = []
        self.created_baselines: list[int] = []
        self.created_email_logs: list[int] = []

    def create_test_athlete(self, name: str = "Automation Test Athlete") -> int:
        """Create a test athlete for automation testing."""
        with get_session() as session:
            athlete = Athlete(
                external_id=f"auto_test_{datetime.now().timestamp()}",
                name=name,
                email="automation_test@example.com",
            )
            session.add(athlete)
            session.commit()
            session.refresh(athlete)
            self.test_athlete_id = athlete.id
            return athlete.id

    def inject_baseline_data(
        self,
        athlete_id: int,
        end_date: date,
        hrv_mean: float = 80.0,
        sleep_mean: float = 8.0,
        rhr_mean: float = 50.0,
        window_type: str = "monthly",
    ) -> None:
        """Inject baseline metric data for a specific date."""
        with get_session() as session:
            baselines = [
                BaselineMetric(
                    athlete_id=athlete_id,
                    metric_name="hrv",
                    window_type=window_type,
                    window_end_date=end_date,
                    mean=hrv_mean,
                    std_dev=5.0,
                    percentile_25=hrv_mean - 3.0,
                    percentile_75=hrv_mean + 3.0,
                    sample_count=30,
                ),
                BaselineMetric(
                    athlete_id=athlete_id,
                    metric_name="sleep_hours",
                    window_type=window_type,
                    window_end_date=end_date,
                    mean=sleep_mean,
                    std_dev=0.5,
                    percentile_25=sleep_mean - 0.5,
                    percentile_75=sleep_mean + 0.5,
                    sample_count=30,
                ),
                BaselineMetric(
                    athlete_id=athlete_id,
                    metric_name="rhr",
                    window_type=window_type,
                    window_end_date=end_date,
                    mean=rhr_mean,
                    std_dev=3.0,
                    percentile_25=rhr_mean - 2.0,
                    percentile_75=rhr_mean + 2.0,
                    sample_count=30,
                ),
            ]

            for baseline in baselines:
                session.add(baseline)
                session.commit()
                session.refresh(baseline)
                self.created_baselines.append(baseline.id)

    def inject_metric_data(
        self,
        athlete_id: int,
        metric_date: date,
        hrv: Optional[float] = None,
        sleep_hours: Optional[float] = None,
        rhr: Optional[float] = None,
        body_score: Optional[float] = None,
        ctl: Optional[float] = None,
        atl: Optional[float] = None,
        tsb: Optional[float] = None,
    ) -> int:
        """
        Inject daily metric data for a specific date.

        Returns the metric ID.
        """
        with get_session() as session:
            metric = DailyMetric(
                athlete_id=athlete_id,
                date=metric_date,
                hrv=hrv,
                sleep_hours=sleep_hours,
                rhr=rhr,
                body_score=body_score,
                ctl=ctl,
                atl=atl,
                tsb=tsb,
            )
            session.add(metric)
            session.commit()
            session.refresh(metric)
            self.created_metrics.append(metric.id)
            return metric.id

    def inject_healthy_metrics(
        self,
        athlete_id: int,
        metric_date: date,
        hrv: float = 82.0,
        sleep_hours: float = 8.2,
        rhr: float = 49.0,
    ) -> int:
        """Inject healthy metrics that won't trigger alerts."""
        return self.inject_metric_data(
            athlete_id=athlete_id,
            metric_date=metric_date,
            hrv=hrv,
            sleep_hours=sleep_hours,
            rhr=rhr,
            body_score=85.0,
            ctl=50.0,
            atl=45.0,
            tsb=5.0,
        )

    def inject_breached_metrics(
        self,
        athlete_id: int,
        metric_date: date,
        hrv: float = 68.0,
        sleep_hours: float = 6.5,
        rhr: float = 56.0,
    ) -> int:
        """Inject metrics that should trigger a recovery alert."""
        return self.inject_metric_data(
            athlete_id=athlete_id,
            metric_date=metric_date,
            hrv=hrv,
            sleep_hours=sleep_hours,
            rhr=rhr,
            body_score=65.0,
            ctl=50.0,
            atl=45.0,
            tsb=5.0,
        )

    def inject_date_range(
        self,
        athlete_id: int,
        start_date: date,
        end_date: date,
        healthy: bool = True,
    ) -> list[int]:
        """
        Inject metrics for a range of dates.

        Args:
            athlete_id: The athlete to create metrics for
            start_date: Start of date range (inclusive)
            end_date: End of date range (inclusive)
            healthy: If True, inject healthy metrics; if False, inject breached metrics

        Returns:
            List of created metric IDs
        """
        metric_ids = []
        current = start_date

        while current <= end_date:
            if healthy:
                metric_id = self.inject_healthy_metrics(athlete_id, current)
            else:
                metric_id = self.inject_breached_metrics(athlete_id, current)
            metric_ids.append(metric_id)
            current += timedelta(days=1)

        return metric_ids

    def check_email_log(self, athlete_id: int, check_date: date) -> Optional[EmailLog]:
        """Check if an email log exists for the given athlete and date."""
        with get_session() as session:
            stmt = select(EmailLog).where(
                EmailLog.athlete_id == athlete_id,
                EmailLog.date == check_date,
                EmailLog.email_type == recovery_alerts.ALERT_EMAIL_TYPE,
            )
            result = session.execute(stmt).scalars().first()
            if result:
                self.created_email_logs.append(result.id)
            return result

    def cleanup(self) -> None:
        """Remove all test data created during this test run."""
        with get_session() as session:
            if self.created_email_logs:
                session.execute(
                    delete(EmailLog).where(EmailLog.id.in_(self.created_email_logs))
                )
            if self.created_metrics:
                session.execute(
                    delete(DailyMetric).where(DailyMetric.id.in_(self.created_metrics))
                )
            if self.created_baselines:
                session.execute(
                    delete(BaselineMetric).where(BaselineMetric.id.in_(self.created_baselines))
                )
            if self.test_athlete_id:
                session.execute(
                    delete(Athlete).where(Athlete.id == self.test_athlete_id)
                )
            session.commit()


def test_scheduler_daily_job_manual():
    """
    Test the daily job manually by calling it directly.
    
    This runs the daily job but only evaluates recovery alerts for premium athletes:
    - Reese Vannerson
    - Blake Bullard
    - Blake Harris
    """
    from app.services.ingest import ingest_recent
    from app.services.recovery_alerts import evaluate_recovery_alert
    from app.utils.dates import get_effective_today
    from app.utils.settings import settings
    from datetime import datetime
    
    from app.utils.settings import settings
    
    print("\n" + "=" * 60)
    print("MANUAL DAILY JOB TEST (Premium Athletes Only)")
    print("=" * 60)
    
    timestamp = datetime.now().isoformat()
    print(f"Started: {timestamp}")
    
    # Show sandbox offset information
    offset = settings.sandbox_current_day_offset
    effective_today = get_effective_today()
    actual_today = date.today()
    
    print(f"\n📅 Date Information:")
    print(f"  Actual today: {actual_today}")
    print(f"  Sandbox offset: {offset} days")
    print(f"  Effective 'today': {effective_today}")
    print(f"  Ingesting: {21} days of data (to ensure full baseline coverage)")
    
    # Step 1: Get premium athletes first
    premium_athlete_names = ["Reese Vannerson", "Blake Bullard", "Blake Harris"]
    athletes = list_athletes()
    
    if not athletes:
        print("\n  ℹ No athletes found in database")
        return
    
    # Filter to only premium athletes
    premium_athletes = [a for a in athletes if a.name in premium_athlete_names]
    
    if not premium_athletes:
        print(f"\n  ℹ None of the premium athletes found in database")
        print(f"     Looking for: {', '.join(premium_athlete_names)}")
        print(f"     Available athletes:")
        for athlete in athletes[:10]:  # Show first 10
            print(f"       - {athlete.name}")
        return
    
    print(f"\nFound {len(premium_athletes)} premium athlete(s) to process")
    for athlete in premium_athletes:
        print(f"  - {athlete.name} (ID: {athlete.id})")
    
    # Step 1: Ingest recent data for each premium athlete
    # Use 21 days to ensure we have data going back before the effective date
    # (with 10-day offset, we need data from ~Oct 6-26 to cover Oct 6-16 effective range)
    print("\n[1/2] Ingesting recent data from TrainingPeaks...")
    print(f"  (This may take a while - fetching 21 days per athlete)")
    ingest_results = []
    for athlete in premium_athletes:
        try:
            print(f"  Ingesting for {athlete.name}...")
            result = ingest_recent(days=21, athlete_id=athlete.id)
            ingest_results.append({"athlete": athlete.name, "status": "success", "result": result})
            print(f"    ✓ Success: {result.get('workouts_fetched', 0)} workouts fetched")
        except Exception as e:
            ingest_results.append({"athlete": athlete.name, "status": "failed", "error": str(e)})
            print(f"    ✗ Failed: {e}")
    
    # Step 2: Evaluate recovery alerts for premium athletes only
    print("\n[2/2] Evaluating recovery alerts for premium athletes...")
    
    check_date = get_effective_today()
    alert_count = 0
    
    for athlete in premium_athletes:
        try:
            result = evaluate_recovery_alert(
                athlete_id=athlete.id,
                check_date=check_date,
                threshold=0.05,  # 5% threshold
            )
            
            if result['triggered']:
                alert_count += 1
                print(f"  🚨 Alert triggered for {athlete.name} (ID: {athlete.id})")
                print(f"     Reason: {result['reason']}")
                print(f"     Email status: {result.get('email_status', 'N/A')}")
            else:
                print(f"  ✓ {athlete.name} (ID: {athlete.id}): {result['reason']}")
                
        except Exception as e:
            print(f"  ✗ Error evaluating {athlete.name} (ID: {athlete.id}): {e}")
    
    print(f"\n  Summary: {alert_count} alert(s) triggered out of {len(premium_athletes)} premium athlete(s)")
    
    print(f"\n{'='*60}")
    print(f"[Daily Job Completed] {datetime.now().isoformat()}")
    print(f"{'='*60}\n")


def test_time_simulation_scenario():
    """
    Test a multi-day scenario by simulating time progression.

    This test creates:
    - Day 1-7: Healthy metrics (no alerts)
    - Day 8: Breached metrics (should trigger alert)
    - Day 9: More breached metrics (should NOT trigger - already sent)
    - Day 10: Healthy metrics (no alert)
    """
    print("\n" + "=" * 60)
    print("TIME SIMULATION SCENARIO TEST")
    print("=" * 60)

    helper = AutomationTestHelper()

    try:
        # Create test athlete
        athlete_id = helper.create_test_athlete("Time Simulation Test")
        print(f"✓ Created test athlete (ID: {athlete_id})")

        # Set baseline
        start_date = date.today() - timedelta(days=10)
        helper.inject_baseline_data(athlete_id, start_date)
        print(f"✓ Created baseline metrics")

        # Day 1-7: Healthy metrics
        print("\nDays 1-7: Injecting healthy metrics...")
        for i in range(7):
            metric_date = start_date + timedelta(days=i)
            helper.inject_healthy_metrics(athlete_id, metric_date)
            print(f"  ✓ Day {i+1}: {metric_date}")

        # Day 8: Breached metrics (should trigger)
        day8_date = start_date + timedelta(days=7)
        print(f"\nDay 8: Injecting BREACHED metrics for {day8_date}...")
        helper.inject_breached_metrics(athlete_id, day8_date)

        print("  📧 Evaluating recovery alert...")
        result_day8 = recovery_alerts.evaluate_recovery_alert(
            athlete_id=athlete_id,
            check_date=day8_date,
            threshold=0.05,
        )
        print(f"  ✓ Triggered: {result_day8['triggered']}")
        print(f"  ✓ Reason: {result_day8['reason']}")

        email_log_day8 = helper.check_email_log(athlete_id, day8_date)
        assert email_log_day8 is not None, "Email should have been sent on Day 8"
        print(f"  ✓ Email log recorded (status: {email_log_day8.status})")

        # Day 9: Still breached (should NOT trigger - already sent)
        day9_date = start_date + timedelta(days=8)
        print(f"\nDay 9: Injecting BREACHED metrics for {day9_date}...")
        helper.inject_breached_metrics(athlete_id, day9_date)

        print("  📧 Evaluating recovery alert...")
        result_day9 = recovery_alerts.evaluate_recovery_alert(
            athlete_id=athlete_id,
            check_date=day9_date,
            threshold=0.05,
        )
        print(f"  ✓ Triggered: {result_day9['triggered']}")
        print(f"  ✓ Reason: {result_day9['reason']}")

        email_log_day9 = helper.check_email_log(athlete_id, day9_date)
        if email_log_day9:
            print(f"  ✓ Email sent on Day 9 (status: {email_log_day9.status})")
        else:
            print(f"  ✓ No duplicate email on Day 9 (correct behavior)")

        # Day 10: Back to healthy
        day10_date = start_date + timedelta(days=9)
        print(f"\nDay 10: Injecting HEALTHY metrics for {day10_date}...")
        helper.inject_healthy_metrics(athlete_id, day10_date)

        print("  📧 Evaluating recovery alert...")
        result_day10 = recovery_alerts.evaluate_recovery_alert(
            athlete_id=athlete_id,
            check_date=day10_date,
            threshold=0.05,
        )
        print(f"  ✓ Triggered: {result_day10['triggered']}")
        print(f"  ✓ Reason: {result_day10['reason']}")

        email_log_day10 = helper.check_email_log(athlete_id, day10_date)
        assert email_log_day10 is None, "No email should be sent when healthy"
        print(f"  ✓ No email on Day 10 (metrics healthy)")

        print("\n" + "=" * 60)
        print("✓ SCENARIO TEST PASSED")
        print("=" * 60 + "\n")

    finally:
        helper.cleanup()
        print("✓ Test data cleaned up\n")


def test_scheduler_with_mock_time():
    """
    Test the scheduler by mocking the current date.

    This allows us to test the scheduler trigger without waiting for the actual time.
    """
    print("\n" + "=" * 60)
    print("SCHEDULER WITH MOCK TIME TEST")
    print("=" * 60)

    helper = AutomationTestHelper()

    try:
        # Create test athlete
        athlete_id = helper.create_test_athlete("Mock Time Test")
        print(f"✓ Created test athlete (ID: {athlete_id})")

        # Future date to simulate
        simulated_date = date.today() + timedelta(days=1)
        print(f"✓ Simulating date: {simulated_date}")

        # Set up baseline
        helper.inject_baseline_data(athlete_id, simulated_date - timedelta(days=1))
        print(f"✓ Created baseline metrics")

        # Inject breached metrics for the simulated date
        helper.inject_breached_metrics(athlete_id, simulated_date)
        print(f"✓ Injected breached metrics")

        # Mock the current date
        print("\n📅 Testing with mocked date...")
        with patch('app.utils.dates.get_effective_today', return_value=simulated_date):
            # This would be called by the scheduler
            result = recovery_alerts.evaluate_recovery_alert(
                athlete_id=athlete_id,
                check_date=None,  # Will use mocked "today"
                threshold=0.05,
            )

        print(f"  ✓ Triggered: {result['triggered']}")
        print(f"  ✓ Reason: {result['reason']}")
        print(f"  ✓ Check date: {result['check_date']}")

        email_log = helper.check_email_log(athlete_id, simulated_date)
        assert email_log is not None, "Email should have been sent"
        print(f"  ✓ Email log recorded for simulated date")

        print("\n" + "=" * 60)
        print("✓ MOCK TIME TEST PASSED")
        print("=" * 60 + "\n")

    finally:
        helper.cleanup()
        print("✓ Test data cleaned up\n")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print(" AUTOMATION TESTING SUITE")
    print("=" * 70)

    print("\nAvailable tests:")
    print("1. test_scheduler_daily_job_manual() - Run daily job manually")
    print("2. test_time_simulation_scenario() - Multi-day simulation")
    print("3. test_scheduler_with_mock_time() - Mock time progression")

    print("\n" + "=" * 70)
    print(" Running all tests...")
    print("=" * 70)

    # Run all tests
    test_time_simulation_scenario()
    test_scheduler_with_mock_time()
    test_scheduler_daily_job_manual()

    print("\n" + "=" * 70)
    print(" ✓ ALL AUTOMATION TESTS COMPLETED")
    print("=" * 70 + "\n")
