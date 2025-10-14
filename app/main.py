import os
import sys
import streamlit as st
from dotenv import load_dotenv

# Ensure project root is on sys.path (handles launches from subdirs or different CWD)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load .env before importing modules that read environment
load_dotenv()

from app.utils.settings import settings
from app.ui import oauth_view, dashboard_view
from app.data.db import init_db
from app.scheduling.scheduler import start_scheduler

st.set_page_config(page_title="Podium Dashboard", layout="wide")

st.title("Podium Dashboard")

# Initialize database tables (no Alembic path)
init_db()

# Start scheduler once
start_scheduler()

# Determine if OAuth callback params present to force Connect page
query_params = st.query_params
has_oauth_code = bool(query_params.get("code"))

# Persist current page across reruns
if "current_page" not in st.session_state:
    st.session_state.current_page = "Connect TrainingPeaks" if has_oauth_code else "Dashboard"
elif has_oauth_code:
    # Override to ensure token exchange happens even if user was on Dashboard
    st.session_state.current_page = "Connect TrainingPeaks"

page = st.sidebar.selectbox(
    "Page",
    ["Dashboard", "Connect TrainingPeaks"],
    index=0 if st.session_state.current_page == "Dashboard" else 1,
    key="page_select"
)
st.session_state.current_page = page

if st.session_state.current_page == "Connect TrainingPeaks":
    oauth_view.render()
else:
    dashboard_view.render()
