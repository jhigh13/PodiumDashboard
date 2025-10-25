from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import List

from app.models.tables import EmailLog
from app.services import recovery_alerts


@dataclass
class FakeResult:
    rows: List[object]

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class FakeSession:
    def __init__(self, responses: List[FakeResult]):
        self._responses = responses
        self.added = []
        self.commits = 0

    def execute(self, _stmt):
        if not self._responses:
            return FakeResult([])
        return self._responses.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@contextmanager
def _session_scope(session: FakeSession):
    yield session


def test_recovery_alert_triggers_and_logs(monkeypatch):
    metric_row = SimpleNamespace(hrv=70.0, sleep_hours=7.0, rhr=44.0)
    session = FakeSession([
        FakeResult([metric_row]),
        FakeResult([]),
    ])

    monkeypatch.setattr(recovery_alerts, "get_session", lambda: _session_scope(session))

    baseline_means = {"hrv": 80.0, "sleep_hours": 8.0, "rhr": 40.0}

    def fake_get_baseline(_athlete_id, metric_name, _window):
        mean = baseline_means.get(metric_name)
        if mean is None:
            return None
        return SimpleNamespace(mean=mean)

    monkeypatch.setattr(recovery_alerts, "get_baseline", fake_get_baseline)

    sent_messages = []

    def fake_send(to_email, subject, body):
        sent_messages.append((to_email, subject, body))
        return {"status": "mocked"}

    monkeypatch.setattr(recovery_alerts.email_client, "send_daily_summary", fake_send)

    result = recovery_alerts.evaluate_recovery_alert(
        athlete_id=1,
        check_date=date(2025, 10, 18),
        threshold=0.05,
    )

    assert result["triggered"] is True
    assert result["reason"] == "sleep_and_hrv_rhr_breach"
    assert sent_messages and "Recovery Alert" in sent_messages[0][1]
    assert session.commits == 1
    assert session.added and isinstance(session.added[0], EmailLog)
    assert session.added[0].email_type == recovery_alerts.ALERT_EMAIL_TYPE


def test_recovery_alert_skips_without_baseline(monkeypatch):
    metric_row = SimpleNamespace(hrv=70.0, sleep_hours=7.0, rhr=44.0)
    session = FakeSession([
        FakeResult([metric_row]),
    ])

    monkeypatch.setattr(recovery_alerts, "get_session", lambda: _session_scope(session))
    monkeypatch.setattr(recovery_alerts, "get_baseline", lambda *_args: None)

    sent_messages = []
    monkeypatch.setattr(
        recovery_alerts.email_client,
        "send_daily_summary",
        lambda *_args, **_kwargs: sent_messages.append(_args) or {"status": "mocked"},
    )

    result = recovery_alerts.evaluate_recovery_alert(
        athlete_id=1,
        check_date=date(2025, 10, 18),
        threshold=0.05,
    )

    assert result["triggered"] is False
    assert result["reason"] == "insufficient_baseline_or_metric"
    assert not sent_messages
    assert session.commits == 0


def test_recovery_alert_triggers_on_sleep_only(monkeypatch):
    metric_row = SimpleNamespace(hrv=80.0, sleep_hours=6.0, rhr=40.0)
    session = FakeSession([
        FakeResult([metric_row]),
        FakeResult([]),
    ])

    monkeypatch.setattr(recovery_alerts, "get_session", lambda: _session_scope(session))

    baseline_means = {"hrv": 80.0, "sleep_hours": 8.0, "rhr": 40.0}

    def fake_get_baseline(_athlete_id, metric_name, _window):
        mean = baseline_means.get(metric_name)
        if mean is None:
            return None
        return SimpleNamespace(mean=mean)

    monkeypatch.setattr(recovery_alerts, "get_baseline", fake_get_baseline)

    sent_messages = []
    monkeypatch.setattr(
        recovery_alerts.email_client,
        "send_daily_summary",
        lambda to_email, subject, body: sent_messages.append((to_email, subject, body)) or {"status": "mocked"},
    )

    result = recovery_alerts.evaluate_recovery_alert(
        athlete_id=1,
        check_date=date(2025, 10, 18),
        threshold=0.05,
    )

    assert result["triggered"] is True
    assert result["reason"] == "sleep_breach"
    assert sent_messages
    assert session.commits == 1


def test_recovery_alert_requires_hrv_and_rhr(monkeypatch):
    metric_row = SimpleNamespace(hrv=70.0, sleep_hours=8.0, rhr=40.0)
    session = FakeSession([
        FakeResult([metric_row]),
    ])

    monkeypatch.setattr(recovery_alerts, "get_session", lambda: _session_scope(session))

    baseline_means = {"hrv": 80.0, "sleep_hours": 8.0, "rhr": 40.0}

    def fake_get_baseline(_athlete_id, metric_name, _window):
        mean = baseline_means.get(metric_name)
        if mean is None:
            return None
        return SimpleNamespace(mean=mean)

    monkeypatch.setattr(recovery_alerts, "get_baseline", fake_get_baseline)

    sent_messages = []
    monkeypatch.setattr(
        recovery_alerts.email_client,
        "send_daily_summary",
        lambda *_args, **_kwargs: sent_messages.append(_args) or {"status": "mocked"},
    )

    result = recovery_alerts.evaluate_recovery_alert(
        athlete_id=1,
        check_date=date(2025, 10, 18),
        threshold=0.05,
    )

    assert result["triggered"] is False
    assert result["reason"] == "conditions_not_met"
    assert not sent_messages
    assert session.commits == 0