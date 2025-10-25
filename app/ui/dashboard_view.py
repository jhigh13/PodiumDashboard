import streamlit as st
from datetime import date, timedelta
from sqlalchemy import select
from app.data.db import get_session
from app.models.tables import Workout, DailyMetric
from app.services.ingest import ingest_recent
from app.services.tokens import get_token
from app.services.athletes import get_or_create_demo_athlete, list_athletes, get_athlete_by_id
from app.services.baseline import get_recent_alerts
from app.services import compliance as compliance_service
from app.utils.dates import get_effective_today
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd


@st.cache_data(ttl=120, show_spinner=False)
def _load_roster(roster_version: int):
    """Cached roster lookup for coach mode selection."""
    roster = list_athletes()
    return [
        {
            "id": athlete.id,
            "name": athlete.name,
            "tp_athlete_id": athlete.tp_athlete_id,
        }
        for athlete in roster
    ]


@st.cache_data(ttl=180, show_spinner=False)
def _load_metrics_range(athlete_id: int, start: date, end: date, version: int, order_desc: bool):
    """Fetch daily metrics for a date range and cache results."""
    order_clause = DailyMetric.date.desc() if order_desc else DailyMetric.date
    with get_session() as session:
        stmt = (
            select(DailyMetric)
            .where(DailyMetric.athlete_id == athlete_id)
            .where(DailyMetric.date >= start)
            .where(DailyMetric.date <= end)
            .order_by(order_clause)
        )
        rows = session.execute(stmt).scalars().all()
    return [
        {
            "date": row.date,
            "rhr": row.rhr,
            "hrv": row.hrv,
            "sleep_hours": row.sleep_hours,
            "body_score": row.body_score,
            "ctl": row.ctl,
            "atl": row.atl,
            "tsb": row.tsb,
        }
        for row in rows
    ]


@st.cache_data(ttl=180, show_spinner=False)
def _load_workouts_range(athlete_id: int, start: date, end: date, version: int):
    """Fetch workouts for dashboard tables and cache results."""
    with get_session() as session:
        stmt = (
            select(Workout)
            .where(Workout.athlete_id == athlete_id)
            .where(Workout.date >= start)
            .where(Workout.date <= end)
            .order_by(Workout.date.desc())
        )
        rows = session.execute(stmt).scalars().all()
    return [
        {
            "date": row.date,
            "sport": row.sport,
            "duration_sec": row.duration_sec,
            "tss": row.tss,
            "intensity_factor": row.intensity_factor,
        }
        for row in rows
    ]


@st.cache_data(ttl=120, show_spinner=False)
def _load_recent_alerts_cached(athlete_id: int, days: int, version: int):
    """Cached recent alerts for display."""
    alerts = get_recent_alerts(athlete_id, days=days)
    return [
        {
            "alert_date": alert.alert_date,
            "message": alert.message,
            "severity": alert.severity,
            "metric_name": alert.metric_name,
            "alert_type": alert.alert_type,
        }
        for alert in alerts
    ]


@st.cache_data(ttl=60, show_spinner=False)
def _load_compliance_snapshot(athlete_id: int, day: date, version: int):
    """Fetch workout compliance summary for a specific day."""
    return compliance_service.get_compliance_for_day(athlete_id, day)


def _format_metric_value(value, unit: str | None) -> str:
    if value is None or value == "" or value == "—":
        return "—"
    if isinstance(value, (int, float)):
        if unit in {"yards", "yard", "yd"}:
            return f"{int(round(value))}"
        if unit in {"miles", "mi"}:
            return f"{value:.2f}".rstrip("0").rstrip(".")
        if unit in {"min"}:
            return f"{value:.1f}"
        if unit == "mph":
            return f"{value:.1f}"
        if unit in {"W", "watts"}:
            return f"{int(round(value))}"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _invalidate_data_caches():
    """Increment data version and clear cached data payloads."""
    st.session_state.setdefault("data_version", 0)
    st.session_state["data_version"] += 1
    _load_metrics_range.clear()
    _load_workouts_range.clear()
    _load_recent_alerts_cached.clear()


def _invalidate_roster_cache():
    """Increment roster version to refresh cached roster."""
    st.session_state.setdefault("roster_version", 0)
    st.session_state["roster_version"] += 1
    _load_roster.clear()


def render():
    st.header("Athlete Dashboard")
    st.session_state.setdefault("data_version", 0)
    st.session_state.setdefault("roster_version", 0)
    # Coach mode: allow selecting athlete
    mode = st.sidebar.radio("Mode", ["Athlete", "Coach"], horizontal=True)
    athlete = get_or_create_demo_athlete()
    if mode == "Coach":
        # Optional: fetch roster from TP
        if st.sidebar.button("Fetch TP Roster", help="Requires coach:athletes scope; upserts athletes into roster"):
            from app.services.coach_roster import sync_coach_roster
            try:
                import time
                start = time.time()
                with st.spinner("Fetching roster from TrainingPeaks (may take up to 30s)..."):
                    summary = sync_coach_roster(athlete.id)
                elapsed = time.time() - start
                st.sidebar.success(f"Fetched {summary['count']} athletes in {elapsed:.1f}s")
                if summary.get('athletes'):
                    with st.sidebar.expander("Sample (up to 10)", expanded=False):
                        st.sidebar.json(summary['athletes'])
                _invalidate_roster_cache()
            except RuntimeError as e:
                st.sidebar.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.sidebar.error(f"Roster fetch failed: {e}")

        roster_data = _load_roster(st.session_state["roster_version"])
        if not roster_data:
            st.sidebar.info("No athletes in roster yet. The app will use the demo athlete until a roster is synced.")
        else:
            display = [
                f"{entry['name'] or 'Unnamed'} (id:{entry['id']}{' TP:'+str(entry['tp_athlete_id']) if entry['tp_athlete_id'] else ''})"
                for entry in roster_data
            ]
            selection = st.sidebar.selectbox("Select Athlete", options=display, index=0)
            selected_idx = display.index(selection)
            athlete_id = roster_data[selected_idx]["id"]
            athlete = get_athlete_by_id(athlete_id)
    effective_today = get_effective_today()
    # TrainingPeaks has a 45-day maximum for single API calls
    days = st.sidebar.slider("Days", min_value=3, max_value=45, value=7)
    
    # Historical Sync Section
    st.sidebar.markdown("---")
    st.sidebar.subheader("📊 Historical Data")
    if st.sidebar.button("Sync Last 365 Days", help="One-time fetch of past year for baseline calculations"):
        from app.services.ingest import ingest_historical_full
        try:
            with st.spinner("Fetching 365 days of data (9 segments, 45-day chunks)..."):
                # 9 segments = ~40 days each, staying within TP's 45-day limit
                summary = ingest_historical_full(days_back=365, athlete_id=athlete.id, segments=9)
            _invalidate_data_caches()
            st.sidebar.success(
                f"✅ Complete! {summary['metrics_saved']} metrics saved, {summary['workouts_inserted']} workouts inserted"
            )
            # Optionally show details
            with st.sidebar.expander("Sync Summary", expanded=False):
                st.sidebar.json(summary)
        except RuntimeError as e:
            st.sidebar.error(str(e))

    # Token status banner
    token_row = get_token(athlete.id)
    if token_row:
        expires = getattr(token_row, "expires_at", None)
        remaining = None
        status_detail = ""
        if expires:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            delta = expires - now
            remaining = int(delta.total_seconds() // 60)
            if remaining < 0:
                status_detail = " (expired)"
            elif remaining < 5:
                status_detail = f" (expires in {remaining} min)"
            else:
                status_detail = f" (≈{remaining} min left)"
        st.markdown(f"✅ TrainingPeaks token present{status_detail}.")
    else:
        # Try to find a coach token for fallback (coach mode)
        from app.services.tokens import find_coach_token
        coach_tok = find_coach_token()
        if coach_tok:
            expires = getattr(coach_tok, "expires_at", None)
            status_detail = ""
            if expires:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                delta = expires - now
                remaining = int(delta.total_seconds() // 60)
                if remaining < 0:
                    status_detail = " (coach token expired)"
                elif remaining < 5:
                    status_detail = f" (coach token expires in {remaining} min)"
                else:
                    status_detail = f" (coach token ≈{remaining} min left)"
            st.markdown(f"✅ Using coach token for API access{status_detail}.")
        else:
            st.markdown("❌ No TrainingPeaks token. Go to **Connect TrainingPeaks** page.")
    # Calculate baselines button
    if st.button("Calculate Baselines", help="Compute baseline metrics from historical data"):
        from app.services.baseline import calculate_baselines
        with st.spinner("Calculating baselines..."):
            results = calculate_baselines(athlete.id)
        if results:
            st.success(f"✅ Baselines calculated for {len(results)} metrics")
            
            # Display baseline results
            with st.expander("📊 Baseline Calculation Results", expanded=True):
                for metric_name, windows in results.items():
                    st.markdown(f"**{metric_name.upper()}**")
                    baseline_table = []
                    for window_name, stats in windows.items():
                        baseline_table.append({
                            "Window": window_name.capitalize(),
                            "Mean": f"{stats['mean']:.2f}",
                            "Std Dev": f"{stats['std_dev']:.2f}",
                            "Samples": stats['sample_count']
                        })
                    st.dataframe(baseline_table, hide_index=True, width="stretch")
        else:
            st.warning("⚠️ Need more historical data. Run 'Sync Last 365 Days' first.")
    
    # Display recent alerts
    recent_alerts = _load_recent_alerts_cached(
        athlete.id,
        days=7,
        version=st.session_state["data_version"],
    )
    if recent_alerts:
        st.markdown("### 🔔 Recent Alerts")
        for alert in recent_alerts[:5]:  # Show top 5
            severity_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(alert["severity"], "⚪")
            st.markdown(f"{severity_emoji} **{alert['alert_date']}**: {alert['message']}")
        with st.expander("View All Alerts"):
            alert_data = [
                {
                    "Date": a["alert_date"],
                    "Metric": a["metric_name"].upper(),
                    "Type": a["alert_type"],
                    "Severity": a["severity"],
                    "Message": a["message"],
                }
                for a in recent_alerts
            ]
            st.dataframe(alert_data, hide_index=True, width="stretch")
    
    compliance_snapshot = _load_compliance_snapshot(
        athlete.id,
        effective_today,
        st.session_state["data_version"],
    )

    st.markdown("### 🧭 Workout Compliance")
    compliance_records = (compliance_snapshot or {}).get("records") if compliance_snapshot else None
    if compliance_records:
        requested_date = compliance_snapshot.get("requested_date")
        record_date = compliance_snapshot.get("workout_date")
        matched_exact = compliance_snapshot.get("is_exact_match", True)

        if not matched_exact and requested_date and record_date:
            st.caption(
                f"Showing the most recent stored evaluations ({record_date}) because no compliance data exists for {requested_date}."
            )

        for idx, record in enumerate(compliance_records):
            sport_display = (record.get("sport") or "–").title()
            score_val = record.get("overall_score")
            score_display = f"{score_val:.0f}" if isinstance(score_val, (int, float)) else "—"
            notes_display = record.get("notes") or ""

            colc1, colc2 = st.columns([1, 1])
            with colc1:
                st.metric("Date", record.get("workout_date") or record_date or requested_date)
                st.metric("Sport", sport_display)
            with colc2:
                st.metric("Score", score_display)
                if notes_display:
                    st.caption(f"Notes: {notes_display}")

            metrics_rows = record.get("metrics") or []
            if metrics_rows:
                table = []
                for row in metrics_rows:
                    unit = row.get("unit") or ""
                    table.append(
                        {
                            "Metric": row.get("metric", "").title(),
                            "Planned": _format_metric_value(row.get("planned"), unit),
                            "Actual": _format_metric_value(row.get("actual"), unit),
                            "Unit": unit,
                            "Rating": (row.get("rating") or "—").title() if row.get("rating") else "—",
                        }
                    )
                st.dataframe(table, hide_index=True, width="stretch")
            else:
                st.info("No evaluatable metrics recorded for this workout yet.")

            if idx < len(compliance_records) - 1:
                st.markdown("---")
    else:
        st.info(
            "No workout compliance summary for the selected day. Sync to fetch the latest workout plan and results."
        )

    # Recovery Metrics Trend Charts
    st.markdown("---")
    st.subheader("📈 Recovery Metrics Trends")
    
    # Fetch metrics data for charts (365 days for rolling calculations)
    chart_end = effective_today
    chart_start = chart_end - timedelta(days=365)
    
    chart_metrics = _load_metrics_range(
        athlete.id,
        chart_start,
        chart_end,
        st.session_state["data_version"],
        order_desc=False,
    )

    if chart_metrics and len(chart_metrics) >= 7:
        # Create dataframe for rolling calculations
        df = pd.DataFrame(chart_metrics)
        if 'sleep_hours' in df.columns:
            df['sleep'] = df['sleep_hours']
        df.sort_values('date', inplace=True)
        
        # Debug: Show data summary
        with st.expander("🔍 Chart Data Debug", expanded=False):
            st.write(f"**Total data points:** {len(df)}")
            st.write(f"**Date range:** {df['date'].min()} to {df['date'].max()}")
            st.write(f"**HRV values:** {df['hrv'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**RHR values:** {df['rhr'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**Sleep values:** {df['sleep'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**HRV range:** {df['hrv'].min():.1f} - {df['hrv'].max():.1f}" if df['hrv'].notna().any() else "**HRV range:** No data")
            st.write(f"**RHR range:** {df['rhr'].min():.1f} - {df['rhr'].max():.1f}" if df['rhr'].notna().any() else "**RHR range:** No data")
            st.dataframe(df.tail(10), width="stretch")
        
        # Calculate rolling averages
        df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()
        df['hrv_30d'] = df['hrv'].rolling(window=30, min_periods=1).mean()
        df['hrv_90d'] = df['hrv'].rolling(window=90, min_periods=1).mean()
        df['hrv_365d'] = df['hrv'].rolling(window=365, min_periods=1).mean()
        
        df['rhr_7d'] = df['rhr'].rolling(window=7, min_periods=1).mean()
        df['rhr_30d'] = df['rhr'].rolling(window=30, min_periods=1).mean()
        df['rhr_90d'] = df['rhr'].rolling(window=90, min_periods=1).mean()
        df['rhr_365d'] = df['rhr'].rolling(window=365, min_periods=1).mean()
        
        # Calculate weekly average sleep (7-day rolling)
        df['sleep_weekly'] = df['sleep'].rolling(window=7, min_periods=1).mean()
        
        # Filter to last 90 days for display (full year used for calculations)
        display_days = 90
        df_display = df[df['date'] >= (chart_end - timedelta(days=display_days))].copy()
        
        # Check if we have any valid data for charts
        has_hrv_data = df_display['hrv'].notna().any()
        has_rhr_data = df_display['rhr'].notna().any()
        has_sleep_data = df_display['sleep'].notna().any()
        
        if not has_hrv_data:
            st.warning("⚠️ No HRV data available in the last 90 days. HRV chart will be empty.")
        if not has_rhr_data:
            st.warning("⚠️ No RHR data available in the last 90 days. RHR chart will be empty.")
        
        # Chart 1: HRV with Sleep
        fig_hrv = make_subplots(specs=[[{"secondary_y": True}]])
        
        # HRV lines
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['hrv_365d'], 
                      name='HRV 365-day', line=dict(color='#1f77b4', width=2.5)),
            secondary_y=False
        )
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['hrv_90d'], 
                      name='HRV 90-day', line=dict(color='#4a90e2', width=2)),
            secondary_y=False
        )
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['hrv_30d'], 
                      name='HRV 30-day', line=dict(color='#7ab8ff', width=2)),
            secondary_y=False
        )
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['hrv_7d'], 
                      name='HRV 7-day', line=dict(color='#a8d5ff', width=2)),
            secondary_y=False
        )
        
        # Weekly average sleep (prominent line)
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['sleep_weekly'], 
                      name='Avg Sleep (weekly)', 
                      line=dict(color='#9467bd', width=4, dash='dash')),
            secondary_y=True
        )
        
        fig_hrv.update_xaxes(title_text="Date")
        fig_hrv.update_yaxes(title_text="HRV (ms)", secondary_y=False)
        fig_hrv.update_yaxes(title_text="Sleep Hours", secondary_y=True)
        fig_hrv.update_layout(
            title="HRV Rolling Averages with Weekly Sleep",
            hovermode='x unified',
            height=500,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        st.plotly_chart(fig_hrv, width="stretch")
        
        # Chart 2: Resting Heart Rate with Sleep
        fig_rhr = make_subplots(specs=[[{"secondary_y": True}]])
        
        # RHR lines
        fig_rhr.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['rhr_365d'], 
                      name='RHR 365-day', line=dict(color='#8b0000', width=2.5)),
            secondary_y=False
        )
        fig_rhr.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['rhr_90d'], 
                      name='RHR 90-day', line=dict(color='#d62728', width=2)),
            secondary_y=False
        )
        fig_rhr.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['rhr_30d'], 
                      name='RHR 30-day', line=dict(color='#ff7f0e', width=2)),
            secondary_y=False
        )
        fig_rhr.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['rhr_7d'], 
                      name='RHR 7-day', line=dict(color='#ffbb78', width=2)),
            secondary_y=False
        )
        
        # Weekly average sleep (prominent line)
        fig_rhr.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['sleep_weekly'], 
                      name='Avg Sleep (weekly)', 
                      line=dict(color='#9467bd', width=4, dash='dash')),
            secondary_y=True
        )
        
        fig_rhr.update_xaxes(title_text="Date")
        fig_rhr.update_yaxes(title_text="Resting Heart Rate (bpm)", secondary_y=False)
        fig_rhr.update_yaxes(title_text="Sleep Hours", secondary_y=True)
        fig_rhr.update_layout(
            title="Resting Heart Rate Rolling Averages with Weekly Sleep",
            hovermode='x unified',
            height=500,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        st.plotly_chart(fig_rhr, width="stretch")
        
    else:
        st.info("📊 Need at least 7 days of metrics data to display trend charts. Run 'Sync Last 365 Days' to populate historical data.")
    
    st.markdown("---")
    if st.button("Manual Sync"):
        try:
            with st.spinner("Syncing..."):
                result = ingest_recent(days=days, athlete_id=athlete.id)
            _invalidate_data_caches()
            st.success("✅ Sync complete!")
            with st.expander("🔍 Sync Details", expanded=True):
                st.write("**Date Range:**", f"{result.get('range', 'unknown')} ({result.get('range_days', '?')} days)")
                st.write("**Workouts:**", f"{result.get('workouts_inserted', 0)} new, {result.get('workout_duplicates', 0)} duplicates")
                st.write("**Metrics:**", f"{result.get('metrics_saved', 0)} saved from {result.get('metrics_fetched', 0)} fetched")
                
                # Show which specific dates had metrics saved
                if result.get('metrics_dates_saved'):
                    dates_saved = result.get('metrics_dates_saved', [])
                    st.write(f"**Dates with metrics saved:** {', '.join(dates_saved)}")
                
                if result.get('metric_field_names'):
                    st.write("**API Metric Fields:**", ", ".join(result.get('metric_field_names', [])))
                if result.get('metrics_raw_sample'):
                    st.json(result.get('metrics_raw_sample', []))

                baseline_summary = result.get('baseline_summary') or {}
                if baseline_summary:
                    metrics_list = ", ".join(sorted(baseline_summary.keys())) or "none"
                    st.write(f"**Baselines updated for:** {metrics_list}")

                alert_info = result.get('recovery_alert') or {}
                if alert_info:
                    if alert_info.get('triggered'):
                        reason = alert_info.get('reason', 'triggered')
                        st.success(f"🚨 Recovery alert triggered ({reason.replace('_', ' ')}) and dispatched")
                    else:
                        st.info(f"Recovery alert check: {alert_info.get('reason', 'no alert')}")

                latest_compliance = result.get('latest_compliance') or {}
                compliance_records = latest_compliance.get('records') or []
                if compliance_records:
                    primary = compliance_records[0]
                    score_val = primary.get('overall_score')
                    display_date = primary.get('workout_date') or latest_compliance.get('workout_date') or latest_compliance.get('requested_date')
                    if score_val is not None:
                        st.write("**Latest compliance score:**", f"{score_val:.0f} (date: {display_date})")
                    if primary.get('notes'):
                        st.write("**Notes:**", primary['notes'])
                    if not latest_compliance.get('is_exact_match', True) and latest_compliance.get('requested_date'):
                        st.caption(
                            f"Compliance fell back to {latest_compliance.get('workout_date')} "
                            f"because nothing was stored for {latest_compliance['requested_date']}"
                        )
        except RuntimeError as e:
            msg = str(e)
            if "No OAuth token" in msg:
                st.error("No TrainingPeaks token found. Go to 'Connect TrainingPeaks' to authorize.")
                if st.button("Go to Connect Page"):
                    st.session_state.current_page = "Connect TrainingPeaks"
                    st.experimental_rerun()
            else:
                st.error(msg)

    end = effective_today
    start = end - timedelta(days=days-1)

    workouts = _load_workouts_range(athlete.id, start, end, st.session_state["data_version"])
    metrics = _load_metrics_range(athlete.id, start, end, st.session_state["data_version"], order_desc=True)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Recent Workouts")
        if not workouts:
            st.info("No workouts stored.")
        else:
            data = [
                {
                    "Date": w["date"],
                    "Sport": w["sport"],
                    "Dur (min)": round(((w.get("duration_sec") or 0)/60), 1),
                    "TSS": w.get("tss"),
                    "IF": w.get("intensity_factor"),
                }
                for w in workouts
            ]
            st.dataframe(data, hide_index=True, width="stretch")
    with col2:
        st.subheader("Daily Metrics")
        if not metrics:
            st.info("No metrics stored for this period.")
        else:
            # Show most recent metric in summary cards
            latest = metrics[0]
            col2a, col2b = st.columns(2)
            with col2a:
                st.metric("RHR", latest.get("rhr") if latest.get("rhr") else "—")
                st.metric("HRV", latest.get("hrv") if latest.get("hrv") else "—")
                st.metric("Sleep (h)", latest.get("sleep_hours") if latest.get("sleep_hours") else "—")
            with col2b:
                st.metric("CTL", latest.get("ctl") if latest.get("ctl") else "—")
                st.metric("ATL", latest.get("atl") if latest.get("atl") else "—")
                st.metric("TSB", latest.get("tsb") if latest.get("tsb") else "—")
            
            # Show all metrics in table
            if len(metrics) > 1:
                st.caption(f"Showing {len(metrics)} metric entries")
                metric_data = [
                    {
                        "Date": m["date"],
                        "RHR": m.get("rhr"),
                        "HRV": m.get("hrv"),
                        "Sleep": m.get("sleep_hours"),
                        "CTL": m.get("ctl"),
                        "ATL": m.get("atl"),
                        "TSB": m.get("tsb"),
                    }
                    for m in metrics
                ]
                st.dataframe(metric_data, hide_index=True, width="stretch")
