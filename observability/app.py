"""Observability dashboard: reads logs/pipeline.log + db/inventory.db and
renders KPIs, the review queue, flag patterns, and vendor stats.

CURRENTLY: Read-only -- run alongside the CLI pipeline, never in place of it:
    streamlit run observability/app.py
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from observability import data

# Status palette (fixed, never themed) -- from the dataviz skill's reference
# palette. Distinct from any categorical series color so a status never
# impersonates a series.
STATUS_COLORS = {
    "paid": "#0ca30c",         # good
    "needs_review": "#fab219", # warning
    "rejected": "#d03b3b",     # critical
}
STATUS_ICONS = {"paid": "✓", "needs_review": "⚠", "rejected": "✕"}
SEQUENTIAL_BLUE = "#2a78d6"

st.set_page_config(page_title="Invoice Pipeline Observability", layout="wide")
st.title("Invoice Pipeline Observability")

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=False)
refresh_seconds = (
    st.sidebar.slider("Refresh every (seconds)", min_value=2, max_value=30, value=5)
    if auto_refresh
    else None
)


@st.fragment(run_every=refresh_seconds)
def render_dashboard() -> None:
    """Everything below re-runs on its own every `refresh_seconds` when
    auto-refresh is on (via st.fragment's run_every), independent of the
    rest of the page -- so a pipeline run in another terminal shows up here
    without you touching the browser. run_every=None (the default) means no
    auto-rerun, same as before: reload the page to see new results.
    """
    df = data.pipeline_df()

    if df.empty:
        st.info("No invoices processed yet -- run `python3 main.py --invoice_dir=data/invoices` first.")
        return

    _render_body(df)


def _render_body(df: pd.DataFrame) -> None:
    # ---- KPI tiles --------------------------------------------------------
    k = data.kpis(df)
    cols = st.columns(6)
    cols[0].metric("Total processed", k["total"])
    cols[1].metric(f"{STATUS_ICONS['paid']} Paid", k["paid"], f"{k['paid_pct']:.0f}%")
    cols[2].metric(f"{STATUS_ICONS['needs_review']} Needs review", k["needs_review"], f"{k['needs_review_pct']:.0f}%")
    cols[3].metric(f"{STATUS_ICONS['rejected']} Rejected", k["rejected"], f"{k['rejected_pct']:.0f}%")
    cols[4].metric("$ Paid", f"${k['amount_paid']:,.0f}")
    cols[5].metric("$ Flagged", f"${k['amount_flagged']:,.0f}")

    st.divider()

    # ---- Status breakdown ---------------------------------------------------
    left, right = st.columns([1, 1])

    with left:
        st.subheader("Outcome breakdown")
        status_counts = df["status"].value_counts().reindex(["paid", "needs_review", "rejected"]).fillna(0)
        fig = go.Figure(
            go.Bar(
                x=status_counts.values,
                y=[s.replace("_", " ") for s in status_counts.index],
                orientation="h",
                marker_color=[STATUS_COLORS[s] for s in status_counts.index],
                text=[f"{STATUS_ICONS[s]} {int(v)}" for s, v in status_counts.items()],
                textposition="outside",
            )
        )
        fig.update_layout(
            showlegend=False, height=260, margin=dict(l=10, r=10, t=10, b=10),
            xaxis=dict(title=None, showgrid=False), yaxis=dict(title=None),
        )
        st.plotly_chart(fig, width="stretch")

    with right:
        st.subheader("Most common validation flags")
        flags = data.flag_counts_df(df)
        if flags.empty:
            st.caption("No flags raised.")
        else:
            flags = flags.sort_values("count")
            fig = go.Figure(
                go.Bar(
                    x=flags["count"], y=flags["type"], orientation="h",
                    marker_color=SEQUENTIAL_BLUE,
                )
            )
            fig.update_layout(
                showlegend=False, height=260, margin=dict(l=10, r=10, t=10, b=10),
                xaxis=dict(title=None, showgrid=False), yaxis=dict(title=None),
            )
            st.plotly_chart(fig, width="stretch")

    st.divider()

    # ---- Review queue -------------------------------------------------------
    st.subheader("Review queue")
    st.caption("Invoices that did not get paid -- the queue a human would work.")
    status_filter = st.multiselect(
        "Filter by status", options=["needs_review", "rejected"],
        default=["needs_review", "rejected"],
    )
    queue = data.review_queue_df(df)
    if status_filter:
        queue = queue[queue["status"].isin(status_filter)]
    st.dataframe(
        queue.rename(columns={
            "timestamp": "Time", "invoice_number": "Invoice #", "vendor": "Vendor",
            "amount": "Amount", "status": "Status", "approval_reasoning": "Reasoning",
            "source_file": "Source file",
        }),
        width="stretch", hide_index=True,
    )

    st.divider()

    # ---- Vendor breakdown -----------------------------------------------------
    st.subheader("Vendor breakdown")
    vendors = data.vendor_breakdown_df(df)
    if vendors.empty:
        st.caption("No vendor history yet.")
    else:
        st.dataframe(
            vendors.rename(columns={
                "vendor": "Vendor", "invoice_count": "Invoices", "paid_count": "Paid",
                "rejected_count": "Rejected", "avg_amount": "Avg amount",
                "near_threshold_count": "Near-$10K invoices",
            }),
            width="stretch", hide_index=True,
        )
        flagged_vendors = vendors[vendors["near_threshold_count"] >= 2]
        if not flagged_vendors.empty:
            st.warning(
                f"{STATUS_ICONS['needs_review']} Possible structuring pattern: "
                + ", ".join(flagged_vendors["vendor"])
            )


render_dashboard()
