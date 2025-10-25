"""Baseline calculation and deviation detection for athlete metrics."""
from datetime import date, timedelta
from sqlalchemy import select, delete
from app.data.db import get_session
from app.models.tables import DailyMetric, BaselineMetric, MetricAlert
import statistics
from app.utils.dates import get_effective_today


METRIC_CONFIGS = {
    "hrv": {
        "db_field": "hrv",
        "higher_is_better": True,
        "display_name": "HRV",
        "unit": "",
    },
    "rhr": {
        "db_field": "rhr",
        "higher_is_better": False,  # Lower RHR is better
        "display_name": "Resting Heart Rate",
        "unit": "bpm",
    },
    "sleep_hours": {
        "db_field": "sleep_hours",
        "higher_is_better": True,
        "display_name": "Sleep Duration",
        "unit": "hours",
    },
}

# Alert thresholds (standard deviations)
THRESHOLD_WEEKLY = 1.0
THRESHOLD_MONTHLY = 0.75
THRESHOLD_ACUTE = 2.0


def calculate_baselines(athlete_id: int, end_date: date | None = None):
    """Calculate annual, monthly, and weekly baselines for all metrics.
    
    Args:
        athlete_id: ID of athlete
        end_date: End date for calculation window (defaults to today)
    
    Returns:
        dict with baseline summary
    """
    if end_date is None:
        end_date = get_effective_today()
    
    windows = {
        "annual": 365,
        "semiannual": 180,
        "quarterly": 90,
        "monthly": 30,
        "weekly": 7,
    }
    
    results = {}
    
    for window_name, days_back in windows.items():
        start_date = end_date - timedelta(days=days_back)
        
        with get_session() as session:
            # Fetch metrics in date range
            stmt = select(DailyMetric).where(
                DailyMetric.athlete_id == athlete_id,
                DailyMetric.date >= start_date,
                DailyMetric.date <= end_date
            )
            metrics = session.execute(stmt).scalars().all()
            
            if not metrics:
                continue
            
            # Calculate baseline for each metric type
            for metric_name, config in METRIC_CONFIGS.items():
                field_name = config["db_field"]
                values = [getattr(m, field_name) for m in metrics if getattr(m, field_name) is not None]
                
                if len(values) < 3:  # Need at least 3 data points
                    continue
                
                mean = statistics.mean(values)
                std_dev = statistics.stdev(values) if len(values) > 1 else 0
                sorted_values = sorted(values)
                p25 = sorted_values[len(sorted_values) // 4]
                p75 = sorted_values[(3 * len(sorted_values)) // 4]
                
                # Delete old baseline for this window
                session.execute(delete(BaselineMetric).where(
                    BaselineMetric.athlete_id == athlete_id,
                    BaselineMetric.metric_name == metric_name,
                    BaselineMetric.window_type == window_name
                ))
                
                # Store new baseline
                baseline = BaselineMetric(
                    athlete_id=athlete_id,
                    metric_name=metric_name,
                    window_type=window_name,
                    window_end_date=end_date,
                    mean=mean,
                    std_dev=std_dev,
                    percentile_25=p25,
                    percentile_75=p75,
                    sample_count=len(values),
                )
                session.add(baseline)
                session.commit()
                
                results.setdefault(metric_name, {})[window_name] = {
                    "mean": mean,
                    "std_dev": std_dev,
                    "sample_count": len(values),
                }
    
    return results


def get_baseline(athlete_id: int, metric_name: str, window_type: str) -> BaselineMetric | None:
    """Retrieve baseline for specific metric and window."""
    with get_session() as session:
        stmt = select(BaselineMetric).where(
            BaselineMetric.athlete_id == athlete_id,
            BaselineMetric.metric_name == metric_name,
            BaselineMetric.window_type == window_type
        ).order_by(BaselineMetric.created_at.desc())
        return session.execute(stmt).scalars().first()


def calculate_deviation_score(value: float, baseline: BaselineMetric, higher_is_better: bool) -> float:
    """Calculate z-score deviation from baseline.
    
    Returns:
        Positive score = better than baseline
        Negative score = worse than baseline
        0 = at baseline
    """
    if baseline.std_dev == 0:
        return 0.0
    
    z_score = (value - baseline.mean) / baseline.std_dev
    
    # Flip sign if lower is better (e.g., RHR)
    if not higher_is_better:
        z_score = -z_score
    
    return z_score


def get_severity(deviation_score: float) -> str:
    """Convert deviation score to traffic light severity."""
    abs_score = abs(deviation_score)
    
    if abs_score < 0.5:
        return "green"
    elif abs_score < 1.0:
        return "yellow"
    else:
        return "red"


def check_alert_conditions(athlete_id: int, check_date: date | None = None) -> list[MetricAlert]:
    """Check if any metrics trigger alerts for the athlete.
    
    Args:
        athlete_id: ID of athlete to check
        check_date: Date to check (defaults to today)
    
    Returns:
        List of MetricAlert objects created
    """
    if check_date is None:
        check_date = date.today()
    
    alerts = []
    
    with get_session() as session:
        # Get today's metrics
        stmt = select(DailyMetric).where(
            DailyMetric.athlete_id == athlete_id,
            DailyMetric.date == check_date
        )
        today_metric = session.execute(stmt).scalars().first()
        
        if not today_metric:
            return alerts
        
        # Check each metric
        for metric_name, config in METRIC_CONFIGS.items():
            current_value = getattr(today_metric, config["db_field"])
            if current_value is None:
                continue
            
            # Check weekly average vs monthly baseline
            weekly_baseline = get_baseline(athlete_id, metric_name, "weekly")
            monthly_baseline = get_baseline(athlete_id, metric_name, "monthly")
            
            if weekly_baseline and monthly_baseline:
                # Weekly average deviation from monthly
                if weekly_baseline.std_dev > 0:
                    weekly_deviation = calculate_deviation_score(
                        weekly_baseline.mean,
                        monthly_baseline,
                        config["higher_is_better"]
                    )
                    
                    if abs(weekly_deviation) > THRESHOLD_WEEKLY:
                        severity = get_severity(weekly_deviation)
                        message = generate_alert_message(
                            config["display_name"],
                            weekly_baseline.mean,
                            monthly_baseline.mean,
                            weekly_deviation,
                            "weekly",
                            config["unit"]
                        )
                        
                        alert = MetricAlert(
                            athlete_id=athlete_id,
                            alert_date=check_date,
                            metric_name=metric_name,
                            alert_type="weekly",
                            current_value=weekly_baseline.mean,
                            baseline_value=monthly_baseline.mean,
                            deviation_score=weekly_deviation,
                            severity=severity,
                            message=message,
                        )
                        session.add(alert)
                        alerts.append(alert)
            
            # Check acute spike (today vs weekly baseline)
            if weekly_baseline:
                acute_deviation = calculate_deviation_score(
                    current_value,
                    weekly_baseline,
                    config["higher_is_better"]
                )
                
                if abs(acute_deviation) > THRESHOLD_ACUTE:
                    severity = get_severity(acute_deviation)
                    message = generate_alert_message(
                        config["display_name"],
                        current_value,
                        weekly_baseline.mean,
                        acute_deviation,
                        "acute",
                        config["unit"]
                    )
                    
                    alert = MetricAlert(
                        athlete_id=athlete_id,
                        alert_date=check_date,
                        metric_name=metric_name,
                        alert_type="acute",
                        current_value=current_value,
                        baseline_value=weekly_baseline.mean,
                        deviation_score=acute_deviation,
                        severity=severity,
                        message=message,
                    )
                    session.add(alert)
                    alerts.append(alert)
        
        session.commit()
    
    return alerts


def generate_alert_message(metric_name: str, current: float, baseline: float, deviation: float, alert_type: str, unit: str) -> str:
    """Generate human-readable alert message."""
    direction = "above" if deviation > 0 else "below"
    percent_diff = abs((current - baseline) / baseline * 100) if baseline != 0 else 0
    
    if alert_type == "weekly":
        return (
            f"{metric_name} this week ({current:.1f}{unit}) is {percent_diff:.0f}% {direction} "
            f"your monthly average ({baseline:.1f}{unit})"
        )
    elif alert_type == "acute":
        return (
            f"Today's {metric_name} ({current:.1f}{unit}) is significantly {direction} "
            f"your weekly average ({baseline:.1f}{unit})"
        )
    else:
        return f"{metric_name}: {current:.1f}{unit} vs baseline {baseline:.1f}{unit}"


def get_recent_alerts(athlete_id: int, days: int = 7) -> list[MetricAlert]:
    """Get recent alerts for an athlete."""
    cutoff_date = date.today() - timedelta(days=days)
    
    with get_session() as session:
        stmt = select(MetricAlert).where(
            MetricAlert.athlete_id == athlete_id,
            MetricAlert.alert_date >= cutoff_date
        ).order_by(MetricAlert.alert_date.desc())
        return session.execute(stmt).scalars().all()
