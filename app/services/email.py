from app.utils.settings import settings

class EmailClient:
    def __init__(self):
        self.api_key = settings.sendgrid_api_key

    def send_daily_summary(self, to_email: str, subject: str, body: str):
        if not self.api_key:
            # Fallback: log only
            print(f"[Email Fallback] To={to_email} Subject={subject}\n{body}")
            return {"status": "logged"}
        # TODO: implement SendGrid send
        return {"status": "sent"}

email_client = EmailClient()
