"""Evaluate and dispatch recovery alerts based on daily metrics and baselines."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

from sqlalchemy import select

from app.data.db import get_session
from app.models.tables import DailyMetric, EmailLog
from app.services.baseline import METRIC_CONFIGS, get_baseline
from app.services.email import email_client
from app.utils.dates import get_effective_today
from app.utils.settings import settings

ALERT_EMAIL_TYPE = "recovery_alert"
BASELINE_WINDOW_PRIORITY = ["annual", "semiannual", "quarterly", "monthly"]
DEFAULT_THRESHOLD = 0.05


@dataclass
class MetricStatus:
    name: str
    current: Optional[float]
    baseline: Optional[float]
    direction: str
    breached: bool
    delta_pct: Optional[float]

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {
            "current": self.current,
            "baseline": self.baseline,
            "direction": self.direction,
            "breached": self.breached,
            "delta_pct": self.delta_pct,
        }


def _get_metric_status(metric_name: str, value: Optional[float], threshold: float, baseline_value: Optional[float]) -> MetricStatus:
    config = METRIC_CONFIGS[metric_name]
    direction = "below" if config["higher_is_better"] else "above"
    breached = False
    delta_pct: Optional[float] = None

    if value is not None and baseline_value not in (None, 0):
        delta_pct = (value - baseline_value) / baseline_value
        if config["higher_is_better"]:
            breached = delta_pct <= -threshold
        else:
            breached = delta_pct >= threshold

    return MetricStatus(
        name=metric_name,
        current=value,
        baseline=baseline_value,
        direction=direction,
        breached=breached,
        delta_pct=delta_pct,
    )


def _select_baseline(athlete_id: int, metric_name: str) -> Optional[float]:
    for window in BASELINE_WINDOW_PRIORITY:
        baseline = get_baseline(athlete_id, metric_name, window)
        if baseline and baseline.mean is not None:
            return baseline.mean
    return None


def _already_sent(session, athlete_id: int, check_date: date) -> bool:
    stmt = select(EmailLog).where(
        EmailLog.athlete_id == athlete_id,
        EmailLog.date == check_date,
        EmailLog.email_type == ALERT_EMAIL_TYPE,
    )
    return session.execute(stmt).first() is not None


def _record_email(session, athlete_id: int, check_date: date, status: str):
    session.add(
        EmailLog(
            athlete_id=athlete_id,
            date=check_date,
            email_type=ALERT_EMAIL_TYPE,
            status=status,
        )
    )
    session.commit()


def _format_metric_line(label: str, metric: MetricStatus) -> str:
    if metric.current is None or metric.baseline is None:
        return f"- {label}: insufficient data"
    change = (metric.delta_pct or 0.0) * 100
    tendency = "below" if metric.delta_pct and metric.delta_pct < 0 else "above"
    return (
        f"- {label}: {metric.current:.2f} vs baseline {metric.baseline:.2f} "
        f"({abs(change):.1f}% {tendency})"
    )


def evaluate_recovery_alert(
    athlete_id: int,
    check_date: Optional[date] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Dict[str, object]:
    """Evaluate recovery metrics and send an alert if all conditions breach."""
    check_date = check_date or get_effective_today()

    with get_session() as session:
        metric = session.execute(
            select(DailyMetric).where(
                DailyMetric.athlete_id == athlete_id,
                DailyMetric.date == check_date,
            )
        ).scalars().first()

        if metric is None:
            return {
                "triggered": False,
                "reason": "no_metric_for_day",
                "check_date": check_date.isoformat(),
                "metrics": {},
            }

        statuses = {
            name: _get_metric_status(
                name,
                getattr(metric, config["db_field"]),
                threshold,
                _select_baseline(athlete_id, name),
            )
            for name, config in METRIC_CONFIGS.items()
            if name in {"hrv", "sleep_hours", "rhr"}
        }

        required_metrics = [statuses["sleep_hours"], statuses["hrv"], statuses["rhr"]]
        if not all(s.baseline not in (None, 0) and s.current is not None for s in required_metrics):
            return {
                "triggered": False,
                "reason": "insufficient_baseline_or_metric",
                "check_date": check_date.isoformat(),
                "metrics": {k: v.to_dict() for k, v in statuses.items()},
            }

        sleep_status = statuses["sleep_hours"]
        hrv_status = statuses["hrv"]
        rhr_status = statuses["rhr"]

        trigger_sleep = sleep_status.breached
        trigger_combo = hrv_status.breached and rhr_status.breached
        triggered = trigger_sleep or trigger_combo

        if triggered:
            if trigger_sleep and trigger_combo:
                trigger_reason = "sleep_and_hrv_rhr_breach"
            elif trigger_sleep:
                trigger_reason = "sleep_breach"
            else:
                trigger_reason = "hrv_rhr_breach"
        else:
            trigger_reason = "conditions_not_met"

        result = {
            "triggered": triggered,
            "reason": trigger_reason,
            "check_date": check_date.isoformat(),
            "metrics": {k: v.to_dict() for k, v in statuses.items()},
        }

        if not triggered:
            return result

        if _already_sent(session, athlete_id, check_date):
            result["reason"] = "already_sent"
            return result

        to_address = settings.head_coach_email
        subject = f"Recovery Alert for {check_date.isoformat()}"
        lead = {
            "sleep_breach": "Sleep hours dropped below baseline threshold.",
            "hrv_rhr_breach": "HRV and Resting HR jointly breached baseline thresholds.",
            "sleep_and_hrv_rhr_breach": "Multiple recovery indicators breached their baselines.",
        }.get(trigger_reason, "Recovery indicators signal elevated fatigue.")

        lines = [
            lead,
            _format_metric_line("Sleep Hours", statuses["sleep_hours"]),
            _format_metric_line("HRV", statuses["hrv"]),
            _format_metric_line("Resting HR", statuses["rhr"]),
            "\nRecommend checking in with the athlete and adjusting training if necessary.",
        ]
        body = "\n".join(lines)

        send_result = email_client.send_daily_summary(to_address, subject, body)
        status = send_result.get("status", "unknown") if isinstance(send_result, dict) else str(send_result)
        _record_email(session, athlete_id, check_date, status)
        result["email_status"] = status
        return result
