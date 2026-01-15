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
from app.services import race_results as race_results_service
from app.services.analytics import (
    compute_leadup_average_sleep_hours,
    compute_leadup_total_training_hours_per_week,
    compute_leadup_training_stats,
)
from app.utils.dates import get_effective_today
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numbers


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


@st.cache_data(ttl=90, show_spinner=False)
def _load_compliance_range(athlete_id: int, start: date, end: date, version: int):
    """Fetch workout compliance summaries for an inclusive date range."""
    return compliance_service.get_compliance_for_range(athlete_id, start, end)


@st.cache_data(ttl=120, show_spinner=False)
def _load_local_races(athlete_id: int, version: int):
    return race_results_service.load_local_race_results(athlete_id)


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
    _load_compliance_snapshot.clear()
    _load_compliance_range.clear()
    _load_local_races.clear()


def _invalidate_roster_cache():
    """Increment roster version to refresh cached roster."""
    st.session_state.setdefault("roster_version", 0)
    st.session_state["roster_version"] += 1
    _load_roster.clear()


def render():
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
    


    '''
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
            st.markdown("❌ No TrainingPeaks token. Go to **Connect TrainingPeaks** page.")'''
    
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

    st.markdown("---")
    st.markdown("### 🏁 Race Performance")

    local_races = _load_local_races(athlete.id, st.session_state["data_version"])
    if not local_races:
        st.info("No local race results yet. Map athlete then sync races via terminal.")
    else:
        picks = race_results_service.pick_best_worst(local_races)
        best = picks.get("best_finished")
        worst_finished = picks.get("worst_finished")
        worst_any = picks.get("worst_including")

        def _race_header(r: dict | None) -> tuple[str, str]:
            if not r:
                return ("—", "—", "—")
            dt = r.get("event_date")
            dts = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
            name = r.get("event_name") or "Unknown"
            return (dts, name)

        def _race_place(r: dict | None) -> str:
            if not r:
                return "—"
            pos = r.get("finish_position")
            return str(pos) if isinstance(pos, int) else "—"

        def _race_status(r: dict | None) -> str:
            if not r:
                return "—"
            return str(r.get("finish_status") or "null")

        colb1, colb2 = st.columns(2)
        with colb1:
            st.metric("Best place (FINISH)", _race_place(best))
            dts, name = _race_header(best)
            st.caption(f"{dts} • {name}")
        with colb2:
            st.metric("Worst place (FINISH)", _race_place(worst_finished))
            dts, name = _race_header(worst_finished)
            st.caption(f"{dts} • {name}")
        # Compact table of all synced races with 28-day lead-up averages
        with st.expander("Race Training Lead-up Averages", expanded=True):
            df = pd.DataFrame(local_races)
            if not df.empty:
                df = df.copy()
                df["event"] = df["event_name"].fillna("").astype(str).str.slice(0, 42)
                df.loc[df["event_name"].fillna("").astype(str).str.len() > 42, "event"] = df["event"].astype(str) + "…"
                df["program"] = df["prog_name"].fillna("").astype(str).str.slice(0, 26)
                df.loc[df["prog_name"].fillna("").astype(str).str.len() > 26, "program"] = df["program"].astype(str) + "…"

                def _stats_cols(row):
                    try:
                        stats = compute_leadup_training_stats(athlete.id, row["event_date"], 28)
                        return (
                            stats["run_miles_per_week"],
                            stats["swim_yards_per_week"],
                            stats["bike_hours_per_week"],
                        )
                    except Exception:
                        return (None, None, None)
                run_mi, swim_yd, bike_hr = [], [], []
                for _, r in df.iterrows():
                    a, b, c = _stats_cols(r)
                    run_mi.append(a)
                    swim_yd.append(b)
                    bike_hr.append(c)
                df["run_mi_wk"] = [round(x, 2) if isinstance(x, (int, float)) else None for x in run_mi]
                df["swim_yd_wk"] = [int(round(x)) if isinstance(x, (int, float)) else None for x in swim_yd]
                df["bike_hr_wk"] = [round(x, 2) if isinstance(x, (int, float)) else None for x in bike_hr]

                # Display place as a number when present, otherwise use finish_status (DNF/LAP/etc).
                pos_num = pd.to_numeric(df.get("finish_position"), errors="coerce")
                status_norm = (
                    df.get("finish_status")
                    .fillna("")
                    .astype(str)
                    .str.upper()
                    .str.strip()
                )
                derived = status_norm
                derived = derived.mask(derived.eq(""), "—")
                derived = derived.mask(derived.isin(["FINISH", "FINISHED", "COMPLETE", "COMPLETED"]), "—")
                derived = derived.apply(lambda s: "LAP" if "LAP" in s else s)

                place = derived.copy()
                has_place = pos_num.notna()
                place.loc[has_place] = pos_num.loc[has_place].astype(int).astype(str)
                df["place"] = place

                df = df[["event", "place", "run_mi_wk", "swim_yd_wk", "bike_hr_wk"]]
                st.dataframe(df, hide_index=True, width="stretch", height=320)
            else:
                st.info("No races to display.")

        # Scatter: placement vs training hours/week, colored by sleep hours (both 28-day averages)
        st.subheader("Placement vs Training (Sleep Color)")
        races_df = pd.DataFrame(local_races).dropna(subset=["event_date"]).copy()
        if races_df.empty:
            st.info("No races available to plot.")
        else:
            races_df["event_date"] = pd.to_datetime(races_df["event_date"]).dt.date
            races_df = races_df.sort_values("event_date")

            points: list[dict[str, object]] = []
            for r in races_df.itertuples(index=False):
                race_date = getattr(r, "event_date", None)
                if not isinstance(race_date, date):
                    continue

                status = str(getattr(r, "finish_status", "") or "").upper().strip()
                finish_statuses = {"", "FINISH", "FINISHED", "COMPLETE", "COMPLETED"}

                place_val = getattr(r, "finish_position", None)
                place_num = int(place_val) if isinstance(place_val, numbers.Integral) else None

                # Treat any explicit non-finish status as non-finish; don't require place_num.
                is_non_finish = (status not in finish_statuses) and (status != "")

                hrs = compute_leadup_total_training_hours_per_week(athlete.id, race_date, 28).get("total_hours_per_week")
                sleep = compute_leadup_average_sleep_hours(athlete.id, race_date, 28).get("avg_sleep_hours")

                points.append(
                    {
                        "date": race_date,
                        "event": str(getattr(r, "event_name", "") or "Unknown"),
                        "place": place_num,
                        "status": status or "—",
                        "non_finish": bool(is_non_finish),
                        "train_hr_wk": float(hrs) if isinstance(hrs, (int, float)) else None,
                        "sleep_hr": float(sleep) if isinstance(sleep, (int, float)) else None,
                    }
                )

            plot_df = pd.DataFrame(points)
            # x is required; also drop 0-hour lead-ups (likely missing/outlier data).
            plot_df = plot_df.dropna(subset=["train_hr_wk"]).copy()
            plot_df = plot_df[plot_df["train_hr_wk"] > 0]
            if plot_df.empty:
                st.info("Not enough data to plot training hours.")
            else:
                # Finishes require an actual placement; non-finishes can be bucketed.
                finishes = plot_df[(plot_df["place"].notna()) & (~plot_df["non_finish"])].copy()
                non_fin = plot_df[plot_df["non_finish"]].copy()

                # Place all non-finish results at a single bucket below the worst finish.
                y_nonfinish = None
                if not finishes.empty:
                    y_nonfinish = int(finishes["place"].max()) + 5
                elif not non_fin.empty:
                    y_nonfinish = 999
                if y_nonfinish is not None and not non_fin.empty:
                    non_fin["place"] = y_nonfinish

                def _hover_text(row: dict[str, object]) -> str:
                    place_display = "NF" if row.get("non_finish") else str(int(row.get("place") or 0))
                    train = row.get("train_hr_wk")
                    sleep_v = row.get("sleep_hr")
                    train_s = f"{float(train):.2f}" if isinstance(train, (int, float)) else "—"
                    sleep_s = f"{float(sleep_v):.2f}" if isinstance(sleep_v, (int, float)) else "—"
                    return (
                        f"{row.get('date')}<br>"
                        f"{row.get('event')}<br>"
                        f"Place: {place_display} ({row.get('status')})<br>"
                        f"Train: {train_s} hr/wk<br>"
                        f"Sleep: {sleep_s} hr"
                    )

                fig = go.Figure()

                def _add_trace(df_in: pd.DataFrame, name: str, symbol: str, show_scale: bool):
                    if df_in.empty:
                        return
                    fig.add_trace(
                        go.Scatter(
                            x=df_in["train_hr_wk"],
                            y=df_in["place"],
                            mode="markers",
                            name=name,
                            marker=dict(
                                size=10,
                                symbol=symbol,
                                color=df_in["sleep_hr"],
                                colorscale="Viridis",
                                showscale=show_scale,
                                colorbar=dict(title="Avg sleep (hr)") if show_scale else None,
                                line=dict(width=0.5, color="rgba(0,0,0,0.35)"),
                            ),
                            text=[_hover_text(row) for row in df_in.to_dict("records")],
                            hovertemplate="%{text}<extra></extra>",
                        )
                    )

                _add_trace(finishes, "Finish", "circle", True)
                _add_trace(non_fin, "Non-finish", "x", False)

                fig.update_layout(
                    height=420,
                    margin=dict(l=10, r=10, t=10, b=10),
                    legend=dict(orientation="h"),
                )
                fig.update_xaxes(title_text="Avg training (hr/week) in last 28 days")
                fig.update_yaxes(title_text="Placement (lower is better)", autorange="reversed")
                st.plotly_chart(fig, width="stretch")

    st.markdown("### 🧭 Workout Compliance")

    def _classify(record: dict) -> dict:
        actual = record.get("actual") or {}
        completed = actual.get("completed") is True
        score = record.get("overall_score")

        if not completed:
            return {"bucket": "missed", "badge": "⚫ Missed", "color": "#6b7280"}

        if not isinstance(score, (int, float)):
            return {"bucket": "unknown", "badge": "⚪ No score", "color": "#9ca3af"}

        if score >= 85:
            return {"bucket": "good", "badge": "🟢 Good", "color": "#16a34a"}
        if score >= 70:
            return {"bucket": "ok", "badge": "🟡 Ok", "color": "#ca8a04"}
        return {"bucket": "bad", "badge": "🔴 Bad", "color": "#dc2626"}

    def _summarize(records: list[dict]) -> dict:
        buckets = {"good": 0, "ok": 0, "bad": 0, "missed": 0, "unknown": 0}
        scores = []
        for r in records:
            c = _classify(r)
            buckets[c["bucket"]] += 1
            if c["bucket"] in {"good", "ok", "bad"} and isinstance(r.get("overall_score"), (int, float)):
                scores.append(float(r["overall_score"]))
        avg = (sum(scores) / len(scores)) if scores else None
        return {"buckets": buckets, "avg_score": avg}

    today_records = (compliance_snapshot or {}).get("records") or []
    last7_start = effective_today - timedelta(days=6)
    last7_end = effective_today
    last7_snapshot = _load_compliance_range(
        athlete.id,
        last7_start,
        last7_end,
        st.session_state["data_version"],
    )
    last7_records = (last7_snapshot or {}).get("records") or []

    today_summary = _summarize(today_records)
    last7_summary = _summarize(last7_records)

    def _today_counts_row(records: list[dict], summary: dict, *, size_px: int = 64):
        b = summary["buckets"]
        total = len(records)

        def _kpi_circle(label: str, count: int, bg: str, fg: str) -> str:
            return (
                f"<div style=\"display:flex;flex-direction:column;align-items:center;gap:8px;\">"
                f"<div title=\"{label}\" style=\"width:{size_px}px;height:{size_px}px;border-radius:999px;"
                f"background:{bg};color:{fg};display:flex;align-items:center;justify-content:center;"
                f"font-weight:950;font-size:24px;line-height:1;\">{count}</div>"
                f"<div style=\"font-size:18px;color:rgba(255,255,255,0.92);font-weight:800;letter-spacing:0.3px;\">{label}</div>"
                f"</div>"
            )

        st.markdown(
            """
<div style="text-align:center;margin:10px 0 6px 0;">
    <div style="font-size:34px;font-weight:950;color:#ffffff;letter-spacing:0.2px;">Workouts Today</div>
    <div style="font-size:18px;color:rgba(255,255,255,0.80);font-weight:750;">"""
            + effective_today.isoformat()
            + """</div>
</div>
            """,
            unsafe_allow_html=True,
        )

        # Centered + evenly spaced layout
        st.markdown(
            """
<div style="display:grid;grid-template-columns:repeat(5, minmax(130px, 1fr));gap:22px;align-items:center;justify-items:center;width:100%;margin:10px 0 14px 0;">
  """
            + _kpi_circle("Workouts", int(total), "#f3f4f6", "#111827")
            + _kpi_circle("Good", int(b.get("good", 0) or 0), "#e6f4ea", "#137333")
            + _kpi_circle("Ok", int(b.get("ok", 0) or 0), "#fff7ed", "#9a3412")
            + _kpi_circle("Bad", int(b.get("bad", 0) or 0), "#fee2e2", "#991b1b")
            + _kpi_circle("Missed", int(b.get("missed", 0) or 0), "#f3f4f6", "#374151")
            + """
</div>
            """,
            unsafe_allow_html=True,
        )

    _today_counts_row(today_records, today_summary)

    def _render_workout_list(records: list[dict]):
        if not records:
            st.info("No workouts in this window.")
            return

        def _sport_name(r: dict) -> str:
            return (r.get("sport") or "").title() or "Other"

        groups: dict[str, list[dict]] = {}
        for r in records:
            groups.setdefault(_sport_name(r), []).append(r)

        sport_order = {"Swim": 0, "Bike": 1, "Run": 2, "Strength": 3}
        for sport in sorted(groups.keys(), key=lambda s: (sport_order.get(s, 99), s)):
            st.markdown(f"#### {sport}")
            for rec in groups[sport]:
                _render_selected_workout(rec)
                st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    def _render_selected_workout(rec: dict):
        c = _classify(rec)
        actual = rec.get("actual") or {}
        completed = actual.get("completed") is True
        sport = (rec.get("sport") or "").title() or "—"
        sport_initial = (sport[:1] or "—").upper()
        date_s = str(rec.get("workout_date") or "—")

        sport_bg = "rgba(255,255,255,0.14)"
        sport_fg = "rgba(255,255,255,0.95)"
        comp_bg = "#e6f4ea" if completed else "#fee2e2"
        comp_fg = "#137333" if completed else "#991b1b"
        comp_char = "✓" if completed else "×"

        st.markdown(
            """
<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0 10px 0;">
  <div style="font-size:18px;font-weight:900;color:#ffffff;">"""
            + date_s
            + """</div>
  <div style="width:28px;height:28px;border-radius:999px;display:flex;align-items:center;justify-content:center;"
       style="background:"""
            + sport_bg
            + """;color:"""
            + sport_fg
            + """;font-weight:950;font-size:14px;">"""
            + sport_initial
            + """</div>
  <div style="font-size:18px;font-weight:850;color:#ffffff;">"""
            + sport
            + """</div>
  <div style="width:28px;height:28px;border-radius:999px;display:flex;align-items:center;justify-content:center;"
       style="background:"""
            + comp_bg
            + """;color:"""
            + comp_fg
            + """;font-weight:950;font-size:16px;">"""
            + comp_char
            + """</div>
  <div style="font-size:18px;font-weight:900;color:#ffffff;">"""
            + str(c["badge"])
            + """</div>
</div>
            """,
            unsafe_allow_html=True,
        )

        metrics_rows = rec.get("metrics") or []
        if not metrics_rows:
            st.info("No planned vs actual metrics recorded for this workout yet.")
            return

        def _metric_sort_key(m: dict) -> tuple[int, str]:
            inc = m.get("include_in_score", True)
            return (0 if inc is False else 1, str(m.get("metric") or ""))

        ordered = sorted(metrics_rows, key=_metric_sort_key)
        out = []
        for m in ordered:
            unit = m.get("unit") or ""
            rating = m.get("rating")
            rating_badge = "🟢" if rating == "good" else "🟡" if rating == "ok" else "🔴" if rating == "bad" else "—"
            out.append(
                {
                    "Metric": str(m.get("metric") or "").title(),
                    "Planned": _format_metric_value(m.get("planned"), unit),
                    "Actual": _format_metric_value(m.get("actual"), unit),
                    "Rating": rating_badge,
                    "_rating": rating or "",
                }
            )

        dfm = pd.DataFrame(out)

        def _row_bg(rating: str) -> str:
            if rating == "good":
                return "#e6f4ea"
            if rating == "ok":
                return "#fff7ed"
            if rating == "bad":
                return "#fee2e2"
            return "#ffffff"

        def _style_rows(s: pd.Series):
            bg = _row_bg(str(s.get("_rating") or ""))
            return [f"background-color: {bg}; color: #111827;"] * len(s)

        styled = (
            dfm.style
            .apply(_style_rows, axis=1)
            .hide(axis="columns", subset=["_rating"])
            .set_properties(**{"border": "1px solid #e5e7eb"})
            .set_table_styles(
                [
                    {"selector": "th", "props": [("background-color", "#f3f4f6"), ("color", "#111827"), ("font-weight", "800"), ("border", "1px solid #e5e7eb")]},
                    {"selector": "td", "props": [("background-color", "#ffffff"), ("color", "#111827"), ("border", "1px solid #e5e7eb")]},
                ]
            )
        )

        # Render a high-contrast light table for readability on dark theme.
        st.dataframe(styled, hide_index=True, width="stretch")

    st.markdown("#### Today")
    _render_workout_list(today_records)

    with st.expander("Last 7 days", expanded=False):
        _render_workout_list(last7_records)

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
                
        # Calculate rolling averages
        df['hrv_7d'] = df['hrv'].rolling(window=7, min_periods=1).mean()
        #df['hrv_30d'] = df['hrv'].rolling(window=30, min_periods=1).mean()
        #df['hrv_90d'] = df['hrv'].rolling(window=90, min_periods=1).mean()
        df['hrv_365d'] = df['hrv'].rolling(window=365, min_periods=1).mean()
        
        df['rhr_7d'] = df['rhr'].rolling(window=7, min_periods=1).mean()
        #df['rhr_30d'] = df['rhr'].rolling(window=30, min_periods=1).mean()
        #df['rhr_90d'] = df['rhr'].rolling(window=90, min_periods=1).mean()
        df['rhr_365d'] = df['rhr'].rolling(window=365, min_periods=1).mean()
        
        # Calculate weekly average sleep (7-day rolling)
        df['sleep_weekly'] = df['sleep'].rolling(window=7, min_periods=1).mean()
        
        # Filter to last 120 days for display (full year used for calculations)
        display_days = 120
        df_display = df[df['date'] >= (chart_end - timedelta(days=display_days))].copy()
        
        # Check if we have any valid data for charts
        has_hrv_data = df_display['hrv'].notna().any()
        has_rhr_data = df_display['rhr'].notna().any()
        has_sleep_data = df_display['sleep'].notna().any()
        
        if not has_hrv_data:
            st.warning("⚠️ No HRV data available in the last 120 days. HRV chart will be empty.")
        if not has_rhr_data:
            st.warning("⚠️ No RHR data available in the last 120 days. RHR chart will be empty.")
        
        # Chart 1: HRV with Sleep
        fig_hrv = make_subplots(specs=[[{"secondary_y": True}]])
        
        # HRV lines
        fig_hrv.add_trace(
            go.Scatter(x=df_display['date'], y=df_display['hrv_365d'], 
                      name='HRV 365-day', line=dict(color='#1f77b4', width=2.5)),
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
        st.info("📊 Need at least 7 days of metrics data to display trend charts. Sync data from the terminal.")
    