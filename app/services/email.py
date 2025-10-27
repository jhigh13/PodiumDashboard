from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.settings import settings

try:
    import resend
except ImportError:  # pragma: no cover - defensive guard if package missing
    resend = None  # type: ignore[assignment]


class EmailClient:
    def __init__(self) -> None:
        self.api_key = settings.resend_api_key
        self.default_from = settings.resend_from_email
        self.default_to = settings.head_coach_email
        
        # Set API key for resend module
        if self.api_key and resend:
            resend.api_key = self.api_key

    def send_text_email(self, to_email: str, subject: str, body: str, from_email: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a plain text email using Resend.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            body: Plain text email body
            from_email: Optional sender email (defaults to HEAD_COACH_EMAIL)
            
        Returns:
            Dictionary with status and response data
        """
        if not to_email:
            return {"status": "error", "error": "missing_recipient"}
        
        if not resend:
            # Fallback logging keeps environments without Resend responsive.
            print(f"[Email Fallback] To={to_email} Subject={subject}\n{body}")
            return {"status": "logged", "reason": "resend_not_installed"}
        
        if not self.api_key:
            print(f"[Email Fallback] To={to_email} Subject={subject}\n{body}")
            return {"status": "logged", "reason": "no_api_key"}
        
        sender = from_email or self.default_from
        
        try:
            # Resend API call
            params = {
                "from": sender,
                "to": [to_email],
                "subject": subject,
                "text": body,
            }
            
            response = resend.Emails.send(params)
            
            # Resend returns a dict with 'id' on success or raises exception
            if response and isinstance(response, dict) and "id" in response:
                return {
                    "status": "sent",
                    "email_id": response["id"],
                    "provider": "resend",
                }
            else:
                return {
                    "status": "error",
                    "error": "unexpected_response",
                    "response": str(response),
                }
                
        except Exception as exc:  # noqa: BLE001 - capture provider errors verbatim
            print(f"[Email Error] {exc}")
            return {"status": "error", "error": str(exc)}

    def send_daily_summary(self, to_email: str, subject: str, body: str) -> Dict[str, Any]:
        """
        Send a daily summary email (alias for send_text_email for backward compatibility).
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            body: Plain text email body
            
        Returns:
            Dictionary with status and response data
        """
        return self.send_text_email(to_email=to_email, subject=subject, body=body)


email_client = EmailClient()
