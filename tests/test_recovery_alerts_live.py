"""
Live integration test for recovery alerts.

This test sends REAL emails using SendGrid and actual database data.
Use this to verify the complete recovery alert workflow end-to-end.

WARNING: This will send actual emails to the configured HEAD_COACH_EMAIL address.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select, delete

from app.data.db import get_session
from app.models.tables import Athlete, DailyMetric, BaselineMetric, EmailLog
from app.services import recovery_alerts
from app.utils.settings import settings


class LiveTestHelper:
    """Helper to set up and tear down test data for live email testing."""

    def __init__(self, athlete_id: Optional[int] = None):
        self.athlete_id = athlete_id
        self.test_athlete_id: Optional[int] = None
        self.created_metrics: list[int] = []
        self.created_baselines: list[int] = []
        self.created_email_logs: list[int] = []

    def create_test_athlete(self, name: str = "Test Athlete - Recovery Alert") -> int:
        """Create a test athlete for this test run."""
        with get_session() as session:
            athlete = Athlete(
                external_id=f"test_recovery_{date.today().isoformat()}",
                name=name,
                email="test@example.com",
            )
            session.add(athlete)
            session.commit()
            session.refresh(athlete)
            self.test_athlete_id = athlete.id
            return athlete.id

    def create_baseline_metrics(
        self,
        athlete_id: int,
        hrv_mean: float = 80.0,
        sleep_mean: float = 8.0,
        rhr_mean: float = 50.0,
        window_type: str = "monthly",
    ) -> None:
        """Create baseline metrics for the athlete."""
        test_date = date.today() - timedelta(days=1)

        with get_session() as session:
            baselines = [
                BaselineMetric(
                    athlete_id=athlete_id,
                    metric_name="hrv",
                    window_type=window_type,
                    window_end_date=test_date,
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
                    window_end_date=test_date,
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
                    window_end_date=test_date,
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

    def create_breached_metrics(
        self,
        athlete_id: int,
        test_date: Optional[date] = None,
        hrv: float = 68.0,  # ~15% below 80
        sleep_hours: float = 6.5,  # ~19% below 8
        rhr: float = 56.0,  # ~12% above 50
    ) -> int:
        """Create daily metrics that will trigger a recovery alert."""
        test_date = test_date or date.today()

        with get_session() as session:
            metric = DailyMetric(
                athlete_id=athlete_id,
                date=test_date,
                hrv=hrv,
                sleep_hours=sleep_hours,
                rhr=rhr,
                body_score=65.0,
                ctl=50.0,
                atl=45.0,
                tsb=5.0,
            )
            session.add(metric)
            session.commit()
            session.refresh(metric)
            self.created_metrics.append(metric.id)
            return metric.id

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

    def verify_email_log(self, athlete_id: int, test_date: date) -> Optional[EmailLog]:
        """Check if an email log was created for this athlete and date."""
        with get_session() as session:
            stmt = select(EmailLog).where(
                EmailLog.athlete_id == athlete_id,
                EmailLog.date == test_date,
                EmailLog.email_type == recovery_alerts.ALERT_EMAIL_TYPE,
            )
            result = session.execute(stmt).scalars().first()
            if result:
                self.created_email_logs.append(result.id)
            return result


def test_live_recovery_alert_email():
    """
    LIVE TEST: Sends a real email to verify recovery alert system.

    This test:
    1. Creates a test athlete
    2. Sets up baseline metrics (HRV=80, Sleep=8hrs, RHR=50)
    3. Creates breached metrics (HRV=68, Sleep=6.5hrs, RHR=56)
    4. Triggers recovery alert evaluation
    5. SENDS A REAL EMAIL via SendGrid
    6. Verifies the email log was recorded
    7. Cleans up all test data

    IMPORTANT: Check your email at: {settings.head_coach_email}
    """
    helper = LiveTestHelper()

    try:
        # Step 1: Create test athlete
        print("\n" + "=" * 60)
        print("LIVE RECOVERY ALERT EMAIL TEST")
        print("=" * 60)
        print(f"Email will be sent to: {settings.head_coach_email}")
        print("=" * 60 + "\n")

        athlete_id = helper.create_test_athlete()
        print(f"✓ Created test athlete (ID: {athlete_id})")

        # Step 2: Create baseline metrics
        helper.create_baseline_metrics(
            athlete_id=athlete_id,
            hrv_mean=80.0,
            sleep_mean=8.0,
            rhr_mean=50.0,
        )
        print("✓ Created baseline metrics (HRV=80, Sleep=8.0hrs, RHR=50)")

        # Step 3: Create breached metrics for today
        test_date = date.today()
        metric_id = helper.create_breached_metrics(
            athlete_id=athlete_id,
            test_date=test_date,
            hrv=68.0,  # 15% below baseline
            sleep_hours=6.5,  # 18.75% below baseline
            rhr=56.0,  # 12% above baseline
        )
        print(f"✓ Created breached metrics for {test_date}")
        print(f"  - HRV: 68.0 (baseline: 80.0, -15%)")
        print(f"  - Sleep: 6.5 hrs (baseline: 8.0, -18.75%)")
        print(f"  - RHR: 56.0 (baseline: 50.0, +12%)")

        # Step 4: Trigger recovery alert
        print("\n📧 Sending recovery alert email...")
        result = recovery_alerts.evaluate_recovery_alert(
            athlete_id=athlete_id,
            check_date=test_date,
            threshold=0.05,  # 5% threshold
        )

        # Step 5: Verify results
        print("\nResults:")
        print(f"  - Triggered: {result['triggered']}")
        print(f"  - Reason: {result['reason']}")
        print(f"  - Email Status: {result.get('email_status', 'N/A')}")

        # Step 6: Verify email log
        email_log = helper.verify_email_log(athlete_id, test_date)
        if email_log:
            print(f"✓ Email log recorded (status: {email_log.status})")
        else:
            print("✗ Email log NOT found in database")

        # Assertions
        assert result["triggered"] is True, "Alert should have triggered"
        assert result["reason"] in [
            "sleep_and_hrv_rhr_breach",
            "sleep_breach",
            "hrv_rhr_breach",
        ], f"Unexpected reason: {result['reason']}"
        assert email_log is not None, "Email log should exist"
        assert result.get("email_status") in ["sent", "logged"], \
            f"Email should have been sent, got: {result.get('email_status')}"

        print("\n" + "=" * 60)
        print("✓ TEST PASSED - Check your email inbox!")
        print("=" * 60 + "\n")

    finally:
        # Cleanup
        print("Cleaning up test data...")
        helper.cleanup()
        print("✓ Test data cleaned up\n")


def test_live_with_existing_athlete():
    """
    Run a live test with an existing athlete from your database.

    Instructions:
    1. Look up an athlete_id from your database
    2. Modify the athlete_id value below
    3. Adjust the baseline and current metric values as needed
    4. Run this test
    """
    # TODO: Set this to a real athlete ID from your database
    ATHLETE_ID = 1  # <-- CHANGE THIS

    helper = LiveTestHelper(athlete_id=ATHLETE_ID)

    try:
        print("\n" + "=" * 60)
        print(f"LIVE TEST WITH ATHLETE ID: {ATHLETE_ID}")
        print("=" * 60)
        print(f"Email will be sent to: {settings.head_coach_email}")
        print("=" * 60 + "\n")

        # Create baselines
        helper.create_baseline_metrics(
            athlete_id=ATHLETE_ID,
            hrv_mean=75.0,  # Adjust to realistic values
            sleep_mean=7.5,
            rhr_mean=52.0,
        )
        print("✓ Created baseline metrics")

        # Create breached metrics
        test_date = date.today()
        helper.create_breached_metrics(
            athlete_id=ATHLETE_ID,
            test_date=test_date,
            hrv=63.0,  # ~16% below baseline
            sleep_hours=6.0,  # ~20% below baseline
            rhr=58.0,  # ~11.5% above baseline
        )
        print(f"✓ Created breached metrics for {test_date}")

        # Trigger alert
        print("\n📧 Sending recovery alert email...")
        result = recovery_alerts.evaluate_recovery_alert(
            athlete_id=ATHLETE_ID,
            check_date=test_date,
            threshold=0.05,
        )

        print(f"\n✓ Alert triggered: {result['triggered']}")
        print(f"  Reason: {result['reason']}")
        print(f"  Email status: {result.get('email_status', 'N/A')}")

        # Verify email log
        email_log = helper.verify_email_log(ATHLETE_ID, test_date)
        assert email_log is not None, "Email log should exist"
        print(f"✓ Email log recorded")

        print("\n" + "=" * 60)
        print("✓ TEST COMPLETE - Check your email!")
        print("=" * 60 + "\n")

    finally:
        print("Cleaning up test data...")
        helper.cleanup()
        print("✓ Cleanup complete\n")


if __name__ == "__main__":
    # Run the live test
    print("\n" + "=" * 70)
    print(" RECOVERY ALERT LIVE EMAIL TEST")
    print("=" * 70)
    print("\nThis will send a REAL email to:", settings.head_coach_email)
    print("\nPress ENTER to continue, or Ctrl+C to cancel...")
    input()

    test_live_recovery_alert_email()

    print("\n" + "=" * 70)
    print(" To test with an existing athlete, edit the file and run:")
    print(" test_live_with_existing_athlete()")
    print("=" * 70 + "\n")
