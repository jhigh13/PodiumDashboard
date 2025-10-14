import streamlit as st
from app.auth.oauth import get_authorization_url, fetch_token
from app.services.athletes import get_or_create_demo_athlete
from app.services.tokens import store_token, get_token
from app.data.db import get_session
from app.models.tables import Athlete
import requests


def render():
    st.header("TrainingPeaks Connection")
    athlete = get_or_create_demo_athlete()

    # Detect callback code in query params
    query_params = st.query_params
    code = query_params.get("code")
    if code and isinstance(code, list):  # streamlit may return list
        code = code[0]
    returned_state = query_params.get("state")
    if isinstance(returned_state, list):
        returned_state = returned_state[0]

    # Role selection (scopes)
    default_role = st.session_state.get("oauth_role", "Athlete")
    role = st.selectbox("Authorization Role", ["Athlete", "Coach"], index=0 if default_role == "Athlete" else 1, help="Choose which type of access to request.")
    st.session_state["oauth_role"] = role
    if role == "Athlete":
        selected_scopes = [
            "athlete:profile",
            "metrics:read",
            "workouts:read",
            "workouts:details",
        ]
    else:
        selected_scopes = [
            "coach:athletes",
            "metrics:read",
            "workouts:read",
            "workouts:details",
        ]
    st.caption("Requested scopes: " + " ".join(selected_scopes))

    if code:
        # Validate state to avoid invalid_request errors
        expected_state = st.session_state.get("oauth_state")
        if expected_state and returned_state and expected_state != returned_state:
            st.error("OAuth state mismatch. Please start the flow again.")
            st.stop()
        
        # Show diagnostic info
        with st.expander("🔍 OAuth Debug Info", expanded=True):
            st.write("**Authorization code received:**", code[:20] + "..." if len(code) > 20 else code)
            st.write("**State matches:**", expected_state == returned_state if expected_state and returned_state else "N/A")
            st.write("**Attempting token exchange...**")
        
        try:
            st.info("Completing OAuth exchange...")
            token = fetch_token(code, scope=None)
            store_token(athlete.id, token)
            st.success("✅ Token stored successfully!")
        except RuntimeError as e:
            st.error(f"Token exchange failed: {e}")
            st.warning("**Troubleshooting steps:**")
            st.markdown("""
            1. Verify your TrainingPeaks credentials are correct in `.env`
            2. Ensure redirect URI matches exactly (including trailing slash)
            3. Try the OAuth flow again (codes expire in 60 minutes)
            4. Contact TrainingPeaks support if issue persists
            """)
            st.stop()
        # Fetch athlete profile (v1)
        headers = {"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json"}
        profile_url = f"{st.session_state.get('tp_api_base_override') or 'https://api.sandbox.trainingpeaks.com'}/v1/athlete/profile"
        try:
            resp = requests.get(profile_url, headers=headers, timeout=20)
            if resp.status_code == 200:
                prof = resp.json()
                with get_session() as session:
                    db_athlete = session.get(Athlete, athlete.id)
                    if db_athlete:
                        db_athlete.tp_athlete_id = prof.get('athleteId') or prof.get('id')
                        db_athlete.name = prof.get('name') or db_athlete.name
                        db_athlete.email = prof.get('email') or db_athlete.email
                        session.commit()
        except Exception as e:  # noqa: BLE001
            st.warning(f"Profile fetch failed: {e}")
        st.success("TrainingPeaks account connected.")
        # Remove code/state from the URL to prevent duplicate exchanges on rerun
        cleaned_params = {k: v for k, v in st.query_params.items() if k not in {"code", "state"}}
        # Force dashboard after successful connect (optional UX choice)
        st.session_state.current_page = "Dashboard"
        st.query_params.update(cleaned_params)
        # Streamlit 1.25+ exposes st.rerun(); older builds had experimental_rerun.
        if hasattr(st, "rerun"):
            st.rerun()
        elif hasattr(st, "experimental_rerun"):
            st.experimental_rerun()
        else:  # pragma: no cover - extreme fallback
            st.write("Please manually refresh the page to continue.")
        return

    token_row = get_token(athlete.id)
    if token_row:
        st.success("Account already connected.")
        with st.expander("Debug Token Info", expanded=False):
            st.write({
                "has_access": bool(token_row.access_token),
                "has_refresh": bool(token_row.refresh_token),
                "expires_at": getattr(token_row, "expires_at", None),
                "scopes": getattr(token_row, "scope", None),
            })
        if st.button("Re-authorize"):
            auth_url, state = get_authorization_url(scope=selected_scopes)
            st.session_state["oauth_state"] = state
            st.link_button("Continue to TrainingPeaks", auth_url)
        return

    if st.button("Start OAuth Flow"):
        auth_url, state = get_authorization_url(scope=selected_scopes)
        st.session_state["oauth_state"] = state
        st.link_button("Continue to TrainingPeaks", auth_url)
    else:
        st.write("Click the button to connect your TrainingPeaks Sandbox account.")
