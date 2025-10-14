from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Date, Float, JSON, Boolean
from sqlalchemy.sql import func
from .base import Base

class Athlete(Base):
    __tablename__ = 'athletes'
    id = Column(Integer, primary_key=True)
    external_id = Column(String, unique=True, index=True)
    tp_athlete_id = Column(Integer, index=True)
    name = Column(String)
    email = Column(String)
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
