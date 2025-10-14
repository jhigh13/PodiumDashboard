from app.utils.settings import settings

def test_settings_defaults():
    assert settings.app_env == 'dev'
    assert settings.daily_job_time == '07:30'
