from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    app_env: str = Field("dev", alias="APP_ENV")
    secret_key: str = Field("dev_secret", alias="SECRET_KEY")
    tp_client_id: str = Field("", alias="TP_CLIENT_ID")
    tp_client_secret: str = Field("", alias="TP_CLIENT_SECRET")
    tp_auth_base: str = Field("https://oauth.sandbox.trainingpeaks.com", alias="TP_AUTH_BASE")
    tp_api_base: str = Field("https://api.sandbox.trainingpeaks.com", alias="TP_API_BASE")
    tp_redirect_uri: str = Field("http://localhost:8501/", alias="TP_REDIRECT_URI")
    tp_scope: str = Field("athlete:profile metrics:read workouts:read workouts:details", alias="TP_SCOPE")
    database_url: str = Field("postgresql+psycopg://postgres.bevhfabnxuqmzdcellwp:6mBe-4ZKA_YWawR@aws-1-us-east-2.pooler.supabase.com:5432/postgres", alias="DATABASE_URL")
    sendgrid_api_key: str = Field("", alias="SENDGRID_API_KEY")
    head_coach_email: str = Field("john.high@usatriathlon.org", alias="HEAD_COACH_EMAIL")
    daily_job_time: str = Field("07:30", alias="DAILY_JOB_TIME")
    sandbox_current_day_offset: int = Field(0, alias="SANDBOX_CURRENT_DAY_OFFSET")

    class Config:
        case_sensitive = False
        env_file = ".env"
        env_file_encoding = "utf-8"

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
