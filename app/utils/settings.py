from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

# Get the project root directory (where .env is located)
PROJECT_ROOT = Path(__file__).parent.parent.parent

class Settings(BaseSettings):
    app_env: str = Field("dev", alias="APP_ENV")
    secret_key: str = Field("dev_secret", alias="SECRET_KEY")
    tp_client_id: str = Field("", alias="TP_CLIENT_ID")
    tp_client_secret: str = Field("", alias="TP_CLIENT_SECRET")
    tp_auth_base: str = Field("https://oauth.trainingpeaks.com", alias="TP_AUTH_BASE")
    tp_api_base: str = Field("https://api.trainingpeaks.com", alias="TP_API_BASE")
    tp_redirect_uri: str = Field("http://localhost:8501/", alias="TP_REDIRECT_URI")
    # Web app redirect URI (FastAPI). Keep Streamlit redirect separate.
    tp_web_redirect_uri: str = Field("http://localhost:8000/oauth/callback", alias="TP_WEB_REDIRECT_URI")
    tp_scope: str = Field("athlete:profile metrics:read workouts:read workouts:details", alias="TP_SCOPE")
    tp_user_agent: str = Field("PodiumDashboard/1.0", alias="TP_USER_AGENT")
    # Never hardcode secrets in source control. Require via environment.
    database_url: str = Field("", alias="DATABASE_URL")
    resend_api_key: str = Field("", alias="RESEND_API_KEY")
    resend_from_email: str = Field("delivered@resend.dev", alias="RESEND_FROM_EMAIL")
    head_coach_email: str = Field("john.high@usatriathlon.org", alias="HEAD_COACH_EMAIL")
    daily_job_time: str = Field("07:30", alias="DAILY_JOB_TIME")
    enable_scheduler: bool = Field(False, alias="ENABLE_SCHEDULER")
    enable_recovery_alert_emails: bool = Field(False, alias="ENABLE_RECOVERY_ALERT_EMAILS")
    enable_daily_summary_emails: bool = Field(False, alias="ENABLE_DAILY_SUMMARY_EMAILS")
    sandbox_current_day_offset: int = Field(0, alias="SANDBOX_CURRENT_DAY_OFFSET")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-5-nano", alias="OPENAI_MODEL")

    # Optional: external canonical triathlon results database (read-only recommended)
    triathlon_database_url: str = Field("", alias="TRIATHLON_DATABASE_URL")

    # Optional: public URL for the rankings dashboard (used in email links)
    rankings_dashboard_url: str = Field("", alias="RANKINGS_DASHBOARD_URL")

    class Config:
        case_sensitive = False
        env_file = str(PROJECT_ROOT / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore unknown .env keys (e.g. TZ, GITPAT, INSCYD_*)

@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[arg-type]
    except ValidationError as e:
        missing = []
        for err in e.errors():
            if err.get("type") == "missing":
                loc = err.get("loc")
                if loc:
                    missing.append(loc[0])
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Check your .env file (copied from .env.example)."
        ) from e

settings = get_settings()
