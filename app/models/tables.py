from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Date, Float, JSON, Boolean, UniqueConstraint
from sqlalchemy.sql import func
from .base import Base

class Athlete(Base):
    __tablename__ = 'athletes'
    id = Column(Integer, primary_key=True)
    external_id = Column(String, unique=True, index=True)
    tp_athlete_id = Column(Integer, index=True)
    name = Column(String)
    email = Column(String)
    # TrainingPeaks daily metrics endpoint is premium-only for many athletes.
    # None = unknown, True = metrics available, False = premium restriction / unavailable.
    tp_metrics_available = Column(Boolean)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class OAuthToken(Base):
    __tablename__ = 'oauth_tokens'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'))
    access_token = Column(String)
    refresh_token = Column(String)
    expires_at = Column(DateTime(timezone=True))
    scope = Column(String)
    provider = Column(String, default='trainingpeaks')
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Workout(Base):
    __tablename__ = 'workouts'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    tp_workout_id = Column(String, index=True)
    date = Column(Date)
    sport = Column(String)
    duration_sec = Column(Integer)
    tss = Column(Float)
    intensity_factor = Column(Float)
    raw_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class DailyMetric(Base):
    __tablename__ = 'daily_metrics'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    date = Column(Date, index=True)
    rhr = Column(Float)
    hrv = Column(Float)
    sleep_hours = Column(Float)
    body_score = Column(Float)
    ctl = Column(Float)
    atl = Column(Float)
    tsb = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Aggregate(Base):
    __tablename__ = 'aggregates'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    date = Column(Date, index=True)
    acute_load = Column(Float)
    chronic_load = Column(Float)
    acwr = Column(Float)
    hrv_baseline = Column(Float)
    rhr_baseline = Column(Float)
    sleep_baseline = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class RiskAssessment(Base):
    __tablename__ = 'risk_assessments'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    date = Column(Date, index=True)
    risk_level = Column(String)
    reasons = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class EmailLog(Base):
    __tablename__ = 'email_log'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    date = Column(Date, index=True)
    email_type = Column(String)
    status = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RecoveryAlertRun(Base):
    __tablename__ = 'recovery_alert_runs'
    __table_args__ = (
        UniqueConstraint('athlete_id', 'alert_date', name='uq_recovery_alert_runs_athlete_date'),
    )

    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    alert_date = Column(Date, index=True)
    threshold = Column(Float)
    triggered = Column(Boolean)
    reason = Column(String)
    metrics = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class BaselineMetric(Base):
    __tablename__ = 'baseline_metrics'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    metric_name = Column(String(50), index=True)  # 'hrv', 'rhr', 'sleep_hours'
    window_type = Column(String(20), index=True)  # 'annual', 'monthly', 'weekly'
    window_end_date = Column(Date, index=True)
    mean = Column(Float)
    std_dev = Column(Float)
    percentile_25 = Column(Float)
    percentile_75 = Column(Float)
    sample_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class MetricAlert(Base):
    __tablename__ = 'metric_alerts'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    alert_date = Column(Date, index=True)
    metric_name = Column(String(50))
    alert_type = Column(String(20))  # 'weekly', 'monthly', 'acute'
    current_value = Column(Float)
    baseline_value = Column(Float)
    deviation_score = Column(Float)
    severity = Column(String(10))  # 'green', 'yellow', 'red'
    message = Column(String)
    acknowledged = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AthleteSurvey(Base):
    __tablename__ = 'athlete_surveys'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    survey_date = Column(Date, index=True)
    sleep_quality = Column(Integer)  # 1-5 scale
    mood = Column(Integer)  # 1-5 scale
    training_feel = Column(Integer)  # 1-5 scale
    stayed_in_range = Column(Boolean)
    race_excitement = Column(Integer)  # 1-5 scale
    notes = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SyncState(Base):
    __tablename__ = 'sync_state'
    id = Column(Integer, primary_key=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), unique=True, index=True)

    last_training_sync_date = Column(Date, index=True)
    last_training_sync_at = Column(DateTime(timezone=True))

    last_race_sync_date = Column(Date, index=True)
    last_race_sync_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WorkoutCompliance(Base):
    __tablename__ = 'workout_compliance'
    id = Column(Integer, primary_key=True)
    workout_id = Column(Integer, ForeignKey('workouts.id', ondelete='CASCADE'), unique=True, index=True)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    workout_date = Column(Date, index=True)
    sport = Column(String(50))
    planned_summary = Column(JSON)
    actual_summary = Column(JSON)
    metrics = Column(JSON)
    overall_score = Column(Float)
    evaluation_notes = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WTODashboardAthleteMap(Base):
    __tablename__ = 'wto_athlete_map'
    id = Column(Integer, primary_key=True)
    podium_athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), unique=True, index=True)
    podium_name = Column(String)
    wto_athlete_id = Column(Integer, index=True)
    wto_full_name = Column(String)
    matched_method = Column(String)  # e.g., 'exact', 'normalized', 'manual'
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WTORaceResult(Base):
    __tablename__ = 'wto_race_results'
    id = Column(Integer, primary_key=True)
    podium_athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True)
    wto_athlete_id = Column(Integer, index=True)
    event_id = Column(Integer, index=True)
    prog_id = Column(Integer, index=True)

    event_date = Column(Date, index=True)
    event_name = Column(String)
    prog_name = Column(String)
    prog_distance_category = Column(String)

    finish_status = Column(String)
    finish_position = Column(Integer)
    position_sort = Column(Integer)
    total_time = Column(String)
    swim_time = Column(String)
    t1_time = Column(String)
    bike_time = Column(String)
    t2_time = Column(String)
    run_time = Column(String)

    raw_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CoachRosterMember(Base):
    __tablename__ = 'coach_roster_members'
    __table_args__ = (
        UniqueConstraint('coach_athlete_id', 'athlete_id', name='uq_coach_roster_member'),
    )

    id = Column(Integer, primary_key=True)
    coach_athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True, nullable=False)
    athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='CASCADE'), index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = 'jobs'

    id = Column(Integer, primary_key=True)
    job_type = Column(String(50), index=True, nullable=False)
    status = Column(String(20), index=True, nullable=False, default='queued')

    # Who requested this job (the logged-in identity; for coach jobs this is the coach athlete id)
    requested_by_athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='SET NULL'), index=True)

    # Optional target athlete (e.g., sync a specific roster athlete)
    target_athlete_id = Column(Integer, ForeignKey('athletes.id', ondelete='SET NULL'), index=True)

    payload = Column(JSON)
    result = Column(JSON)
    error = Column(String)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
