"""Developer/ops dashboard: reads logs/telemetry.log and renders per-stage
latency, failures, and retries -- engineering signals user_observe_app.py doesn't cover.

Read-only, same as user_observe_app.py -- run alongside the CLI pipeline:
    streamlit run observability/dev_observe_app.py
"""

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from observability import data

STATUS_COLORS = {"success": "#0ca30c", "failed": "#d03b3b"}  # good / critical
STAGE_COLOR = "#2a78d6"  # sequential blue -- one series, magnitude not identity

# Telemetry has no dedup (unlike app.py's pipeline_df) -- every run stays
# forever, so a long-running dev instance would otherwise show lifetime
# stats instead of current health. This filter is how you get "how's it
# doing right now" instead of "how's it done ever."
TIME_RANGE_OPTIONS: dict[str, timedelta | None] = {
    "All time": None,
    "Last 15 min": timedelta(minutes=15),
    "Last 1 hour": timedelta(hours=1),
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
}

st.set_page_config(page_title="Developer Dashboard", layout="wide")
st.title("Developer Dashboard")

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=False)
refresh_seconds = (
    st.sidebar.slider("Refresh every (seconds)", min_value=2, max_value=30, value=5)
    if auto_refresh
    else None
)
time_range_label = st.sidebar.selectbox("Time range", list(TIME_RANGE_OPTIONS.keys()), index=0)
time_range_delta = TIME_RANGE_OPTIONS[time_range_label]


@st.fragment(run_every=refresh_seconds)
def render_dashboard() -> None:
    run_df = data.run_telemetry_df()

    if run_df.empty:
        st.info(
            "No telemetry recorded yet -- run `python3 main.py --invoice_dir=data/invoices` "
            "first (telemetry is written from main.py, so standalone `agents/*.py` runs won't show up here)."
        )
        return

    if time_range_delta is not None:
        cutoff = pd.Timestamp.now(tz="UTC") - time_range_delta
        filtered_df = run_df[run_df["timestamp"] >= cutoff]
    else:
        filtered_df = run_df

    if filtered_df.empty:
        st.warning(
            f"No runs in the selected time range ({time_range_label}) -- "
            f"{len(run_df)} total run(s) recorded outside this window."
        )
        return

    _render_body(filtered_df, since=cutoff if time_range_delta is not None else None)


def _render_body(run_df: pd.DataFrame, since: pd.Timestamp | None = None) -> None:
    # ---- KPI tiles ----------------------------------------------------
    k = data.dev_kpis(run_df)
    cols = st.columns(5)
    cols[0].metric("Total runs", k["total_runs"])
    cols[1].metric("✓ Success", k["success"])
    cols[2].metric("✕ Failed", k["failed"], f"{k['error_rate_pct']:.1f}% error rate")
    cols[3].metric("Avg total latency", f"{k['avg_total_duration']:.2f}s")
    cols[4].metric("p95 total latency", f"{k['p95_total_duration']:.2f}s")

    st.divider()

    left, right = st.columns([1, 1])

    # ---- Per-stage latency ---------------------------------------------
    with left:
        st.subheader("Per-stage latency (avg / p95)")
        stats = data.stage_latency_stats(run_df)
        if stats.empty:
            st.caption("No completed stage timings yet.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Bar(
                name="avg", x=stats["stage"], y=stats["avg"], marker_color=STAGE_COLOR,
            ))
            fig.add_trace(go.Bar(
                name="p95", x=stats["stage"], y=stats["p95"], marker_color="#9ec5f4",
            ))
            fig.update_layout(
                barmode="group", height=280, margin=dict(l=10, r=10, t=10, b=10),
                yaxis=dict(title="seconds", showgrid=True, gridcolor="#eee"),
                xaxis=dict(title=None),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig, width="stretch")

    # ---- Run outcome breakdown ------------------------------------------
    with right:
        st.subheader("Run outcomes")
        status_counts = run_df["status"].value_counts().reindex(["success", "failed"]).fillna(0)
        fig = go.Figure(
            go.Bar(
                x=status_counts.values,
                y=list(status_counts.index),
                orientation="h",
                marker_color=[STATUS_COLORS[s] for s in status_counts.index],
                text=[f"{int(v)}" for v in status_counts.values],
                textposition="outside",
            )
        )
        fig.update_layout(
            showlegend=False, height=280, margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(title=None, showgrid=False), yaxis=dict(title=None),
        )
        st.plotly_chart(fig, width="stretch")

    st.divider()

    # ---- Latency over time -----------------------------------------------
    st.subheader("Total latency over time")
    timeline = run_df.sort_values("timestamp")
    fig = go.Figure(
        go.Scatter(
            x=timeline["timestamp"], y=timeline["total_duration"],
            mode="markers+lines", line=dict(color=STAGE_COLOR, width=2),
            marker=dict(size=8, color=STAGE_COLOR),
        )
    )
    fig.update_layout(
        height=260, margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(title="seconds", showgrid=True, gridcolor="#eee"), xaxis=dict(title=None),
    )
    st.plotly_chart(fig, width="stretch")

    st.divider()

    # ---- Failures --------------------------------------------------------
    st.subheader("Failures")
    failures = run_df[run_df["status"] == "failed"][
        ["timestamp", "source_file", "invoice_number", "failed_stage", "error"]
    ]
    if failures.empty:
        st.caption("No failures recorded.")
    else:
        st.dataframe(
            failures.rename(columns={
                "timestamp": "Time", "source_file": "Source file",
                "invoice_number": "Invoice #", "failed_stage": "Failed at",
                "error": "Error",
            }),
            width="stretch", hide_index=True,
        )

    st.divider()

    # ---- Retries -----------------------------------------------------------
    st.subheader("Retries")
    retries = data.retry_df()
    if since is not None and not retries.empty:
        retries = retries[retries["timestamp"] >= since]
    if retries.empty:
        st.caption("No retries recorded.")
    else:
        st.dataframe(
            retries.rename(columns={
                "timestamp": "Time", "source_file": "Source file",
                "stage": "Stage", "attempt": "Attempt #",
            }),
            width="stretch", hide_index=True,
        )


render_dashboard()
