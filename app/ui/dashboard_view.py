import streamlit as st
from datetime import date, timedelta
from sqlalchemy import select
from app.data.db import get_session
from app.models.tables import Workout, DailyMetric
from app.services.ingest import ingest_recent
from app.services.tokens import get_token
from app.services.athletes import get_or_create_demo_athlete, list_athletes, get_athlete_by_id
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd


def render():
    st.header("Athlete Dashboard")
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
                # Refresh roster in-place without forcing a full rerun
                # so the dropdown picks up new entries immediately.
                # We'll re-query roster below after this block.
            except RuntimeError as e:
                st.sidebar.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.sidebar.error(f"Roster fetch failed: {e}")

        roster = list_athletes()
        if not roster:
            st.sidebar.info("No athletes in roster yet. The app will use the demo athlete until a roster is synced.")
        else:
            display = [
                f"{a.name or 'Unnamed'} (id:{a.id}{' TP:'+str(a.tp_athlete_id) if a.tp_athlete_id else ''})"
                for a in roster
            ]
            selection = st.sidebar.selectbox("Select Athlete", options=display, index=0)
            selected_idx = display.index(selection)
            athlete = roster[selected_idx]
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
                    st.dataframe(baseline_table, hide_index=True, use_container_width=True)
        else:
            st.warning("⚠️ Need more historical data. Run 'Sync Last 365 Days' first.")
    
    # Display recent alerts
    from app.services.baseline import get_recent_alerts
    recent_alerts = get_recent_alerts(athlete.id, days=7)
    if recent_alerts:
        st.markdown("### 🔔 Recent Alerts")
        for alert in recent_alerts[:5]:  # Show top 5
            severity_emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(alert.severity, "⚪")
            st.markdown(f"{severity_emoji} **{alert.alert_date}**: {alert.message}")
        with st.expander("View All Alerts"):
            alert_data = [
                {
                    "Date": a.alert_date,
                    "Metric": a.metric_name.upper(),
                    "Type": a.alert_type,
                    "Severity": a.severity,
                    "Message": a.message,
                }
                for a in recent_alerts
            ]
            st.dataframe(alert_data, hide_index=True, width="stretch")
    
    # Recovery Metrics Trend Charts
    st.markdown("---")
    st.subheader("📈 Recovery Metrics Trends")
    
    # Fetch metrics data for charts (365 days for rolling calculations)
    chart_end = date.today()
    chart_start = chart_end - timedelta(days=365)
    
    with get_session() as session:
        chart_stmt = (
            select(DailyMetric)
            .where(DailyMetric.athlete_id == athlete.id)
            .where(DailyMetric.date >= chart_start)
            .where(DailyMetric.date <= chart_end)
            .order_by(DailyMetric.date)
        )
        chart_metrics = session.scalars(chart_stmt).all()
    
    if chart_metrics and len(chart_metrics) >= 7:
        # Create dataframe for rolling calculations
        df = pd.DataFrame([
            {
                'date': m.date,
                'hrv': m.hrv,
                'rhr': m.rhr,
                'sleep': m.sleep_hours
            }
            for m in chart_metrics
        ])
        
        # Debug: Show data summary
        with st.expander("🔍 Chart Data Debug", expanded=False):
            st.write(f"**Total data points:** {len(df)}")
            st.write(f"**Date range:** {df['date'].min()} to {df['date'].max()}")
            st.write(f"**HRV values:** {df['hrv'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**RHR values:** {df['rhr'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**Sleep values:** {df['sleep'].notna().sum()} non-null out of {len(df)}")
            st.write(f"**HRV range:** {df['hrv'].min():.1f} - {df['hrv'].max():.1f}" if df['hrv'].notna().any() else "**HRV range:** No data")
            st.write(f"**RHR range:** {df['rhr'].min():.1f} - {df['rhr'].max():.1f}" if df['rhr'].notna().any() else "**RHR range:** No data")
            st.dataframe(df.tail(10), use_container_width=True)
        
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
        
        st.plotly_chart(fig_hrv, use_container_width=True)
        
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
        
        st.plotly_chart(fig_rhr, use_container_width=True)
        
    else:
        st.info("📊 Need at least 7 days of metrics data to display trend charts. Run 'Sync Last 365 Days' to populate historical data.")
    
    st.markdown("---")
    if st.button("Manual Sync"):
        try:
            with st.spinner("Syncing..."):
                result = ingest_recent(days=days, athlete_id=athlete.id)
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
        except RuntimeError as e:
            msg = str(e)
            if "No OAuth token" in msg:
                st.error("No TrainingPeaks token found. Go to 'Connect TrainingPeaks' to authorize.")
                if st.button("Go to Connect Page"):
                    st.session_state.current_page = "Connect TrainingPeaks"
                    st.experimental_rerun()
            else:
                st.error(msg)

    end = date.today()
    start = end - timedelta(days=days-1)

    with get_session() as session:
        w_stmt = select(Workout).where(Workout.athlete_id == athlete.id, Workout.date >= start, Workout.date <= end).order_by(Workout.date.desc())
        workouts = session.execute(w_stmt).scalars().all()
        # Get all metrics in date range, not just latest
        m_stmt = select(DailyMetric).where(
            DailyMetric.athlete_id == athlete.id,
            DailyMetric.date >= start,
            DailyMetric.date <= end
        ).order_by(DailyMetric.date.desc())
        metrics = session.execute(m_stmt).scalars().all()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Recent Workouts")
        if not workouts:
            st.info("No workouts stored.")
        else:
            data = [
                {
                    "Date": w.date,
                    "Sport": w.sport,
                    "Dur (min)": round((w.duration_sec or 0)/60, 1),
                    "TSS": w.tss,
                    "IF": w.intensity_factor,
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
                st.metric("RHR", latest.rhr if latest.rhr else "—")
                st.metric("HRV", latest.hrv if latest.hrv else "—")
                st.metric("Sleep (h)", latest.sleep_hours if latest.sleep_hours else "—")
            with col2b:
                st.metric("CTL", latest.ctl if latest.ctl else "—")
                st.metric("ATL", latest.atl if latest.atl else "—")
                st.metric("TSB", latest.tsb if latest.tsb else "—")
            
            # Show all metrics in table
            if len(metrics) > 1:
                st.caption(f"Showing {len(metrics)} metric entries")
                metric_data = [
                    {
                        "Date": m.date,
                        "RHR": m.rhr,
                        "HRV": m.hrv,
                        "Sleep": m.sleep_hours,
                        "CTL": m.ctl,
                        "ATL": m.atl,
                        "TSB": m.tsb,
                    }
                    for m in metrics
                ]
                st.dataframe(metric_data, hide_index=True, width="stretch")
