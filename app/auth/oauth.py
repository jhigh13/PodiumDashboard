# Placeholder for OAuth flow implementation using Authlib
"""OAuth helper functions for TrainingPeaks.

Provides authorization URL generation, initial token exchange and refresh with
robust error handling so upstream callers (API layer / UI) can react with a
clear re-authorization prompt when necessary.
"""

from authlib.integrations.requests_client import OAuth2Session
import requests
from app.utils.settings import settings

AUTHORIZE_URL = f"{settings.tp_auth_base}/oauth/authorize"
TOKEN_URL = f"{settings.tp_auth_base}/oauth/token"


def create_session(redirect_uri: str | None = None, token: dict | None = None, scope: str | list[str] | None = None) -> OAuth2Session:
    if isinstance(scope, list):
        scope_str = " ".join(scope)
    else:
        scope_str = scope or settings.tp_scope
    return OAuth2Session(
        client_id=settings.tp_client_id,
        client_secret=settings.tp_client_secret,
        redirect_uri=redirect_uri or settings.tp_redirect_uri,
        scope=scope_str,
        token=token,
        token_endpoint_auth_method="client_secret_post",
    )


def get_authorization_url(scope: str | list[str] | None = None):
    session = create_session(scope=scope)
    uri, state = session.create_authorization_url(AUTHORIZE_URL)
    return uri, state


def _validate_token_dict(token: dict, context: str):
    if not isinstance(token, dict):  # pragma: no cover - defensive
        raise RuntimeError(f"OAuth {context} did not return JSON dictionary. Got: {type(token)}")
    if not token.get("access_token"):
        raise RuntimeError(f"OAuth {context} missing access_token; response keys: {list(token.keys())}")
    return token


def fetch_token(code: str, scope: str | list[str] | None = None):
    """Exchange authorization code for access token.
    
    TrainingPeaks requires:
    - Content-Type: application/x-www-form-urlencoded (not JSON)
    - Code must be URL decoded from callback, then properly encoded in form data
    - All parameters in POST body
    """
    # Streamlit query params come URL-decoded, but we need to ensure proper encoding for POST
    import urllib.parse
    
    # Build form data payload per TrainingPeaks docs
    payload = {
        "grant_type": "authorization_code",
        "code": code,  # requests will handle URL encoding in form data
        "redirect_uri": settings.tp_redirect_uri,
        "client_id": settings.tp_client_id,
        "client_secret": settings.tp_client_secret,
    }
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    
    try:
        # Use requests directly with form encoding (not authlib)
        resp = requests.post(TOKEN_URL, data=payload, headers=headers, timeout=30)
        
        # Log response for diagnostics (without exposing tokens)
        if resp.status_code != 200:
            error_detail = None
            try:
                error_detail = resp.json()
            except Exception:  # noqa: BLE001
                error_detail = resp.text[:500]
            raise RuntimeError(
                f"Token exchange failed (HTTP {resp.status_code}). "
                f"Error: {error_detail}"
            )
        
        token = resp.json()
        return _validate_token_dict(token, "exchange")
        
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"OAuth token exchange network error: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OAuth token exchange failed: {e}") from e


def refresh_token(refresh_token: str):
    """Refresh an access token.
    
    TrainingPeaks requires application/x-www-form-urlencoded for token refresh.
    """
    if not refresh_token:
        raise RuntimeError("No refresh token stored; please re-authorize TrainingPeaks.")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.tp_client_id,
        "client_secret": settings.tp_client_secret,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    
    try:
        resp = requests.post(TOKEN_URL, data=payload, headers=headers, timeout=30)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Token refresh network error: {e}. Please re-authorize.") from e

    if resp.status_code != 200:
        # Attempt to surface JSON error else raw text
        err_detail = None
        try:
            err_detail = resp.json()
        except Exception:  # noqa: BLE001
            err_detail = resp.text.strip()[:300]
        raise RuntimeError(
            f"Token refresh failed (HTTP {resp.status_code}). Detail: {err_detail}. Please re-authorize."
        )
    try:
        token = resp.json()
    except Exception as e:  # noqa: BLE001
        # This mirrors the original user-facing error but with more context.
        snippet = resp.text[:120]
        raise RuntimeError(
            f"Token refresh returned non-JSON body (first 120 chars: {snippet!r}). Please re-authorize."
        ) from e
    return _validate_token_dict(token, "refresh")
