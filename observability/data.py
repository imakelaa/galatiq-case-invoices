"""Read-only data layer for the observability dashboard.

Everything here reads what the pipeline already writes (logs/pipeline.log,
db/inventory.db) -- nothing here writes anything. Kept separate from
app.py (the Streamlit rendering) so this module can be reused by a future
API backend without dragging Streamlit along with it.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from agents import ledger

PIPELINE_LOG = Path(__file__).parent.parent / "logs" / "pipeline.log"
TELEMETRY_LOG = Path(__file__).parent.parent / "logs" / "telemetry.log"


def _read_json_stream(path: Path) -> list[dict]:
    """Parses a file of back-to-back pretty-printed JSON objects (no separators).

    payment.py appends each entry with json.dumps(..., indent=2) -- not a
    JSON array and not newline-delimited single-line JSON -- so a normal
    json.load/jsonlines read doesn't work. json.JSONDecoder.raw_decode
    reads one object at a time and reports how far it got, so repeatedly
    decoding from that offset (skipping whitespace) walks the whole stream.
    """
    if not path.exists():
        return []
    text = path.read_text()
    decoder = json.JSONDecoder()
    entries = []
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(text, idx)
        entries.append(obj)
        idx = end
    return entries


def load_pipeline_entries() -> list[dict]:
    """Every entry ever appended to pipeline.log, oldest first."""
    return _read_json_stream(PIPELINE_LOG)


def pipeline_df(latest_only: bool = True) -> pd.DataFrame:
    """Flattened one-row-per-invoice view of pipeline.log.

    latest_only=True (the default, used for KPIs/queue/flags) keeps only
    the most recent entry per (invoice_number, source_file) -- the same
    "reprocessing overwrites" semantics as the payments ledger, so a
    invoice re-run during debugging isn't double-counted. Pass False to
    get the raw audit trail (every run, including superseded ones).
    """
    entries = load_pipeline_entries()
    rows = []
    for entry in entries:
        invoice = entry.get("invoice", {})
        validation = entry.get("validation", {})
        approval = entry.get("approval", {})
        payment = entry.get("payment", {})
        rows.append(
            {
                "timestamp": entry.get("timestamp"),
                "invoice_number": invoice.get("invoice_number"),
                "source_file": invoice.get("source_file"),
                "vendor": invoice.get("vendor"),
                "amount": invoice.get("amount"),
                "invoice_date": invoice.get("invoice_date"),
                "validation_passed": validation.get("passed"),
                "flags": validation.get("flags", []),
                "approval_decision": approval.get("decision"),
                "approval_reasoning": approval.get("reasoning"),
                "critique_rounds": approval.get("critique_rounds"),
                "status": payment.get("status"),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if latest_only:
        df = df.sort_values("timestamp").drop_duplicates(
            subset=["invoice_number", "source_file"], keep="last"
        )
    return df.sort_values("timestamp", ascending=False).reset_index(drop=True)


def kpis(df: Optional[pd.DataFrame] = None) -> dict:
    """Headline counts/amounts for the top-of-dashboard tiles."""
    df = pipeline_df() if df is None else df
    if df.empty:
        return {
            "total": 0, "paid": 0, "rejected": 0, "needs_review": 0,
            "amount_paid": 0.0, "amount_flagged": 0.0,
        }
    counts = df["status"].value_counts()
    total = len(df)
    paid = int(counts.get("paid", 0))
    rejected = int(counts.get("rejected", 0))
    needs_review = int(counts.get("needs_review", 0))
    return {
        "total": total,
        "paid": paid,
        "rejected": rejected,
        "needs_review": needs_review,
        "paid_pct": paid / total * 100 if total else 0.0,
        "rejected_pct": rejected / total * 100 if total else 0.0,
        "needs_review_pct": needs_review / total * 100 if total else 0.0,
        "amount_paid": float(df.loc[df["status"] == "paid", "amount"].sum()),
        "amount_flagged": float(df.loc[df["status"] != "paid", "amount"].sum()),
    }


def review_queue_df(df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Invoices that didn't get paid -- the queue a human would work, newest first."""
    df = pipeline_df() if df is None else df
    if df.empty:
        return df
    queue = df[df["status"] != "paid"].copy()
    return queue[
        ["timestamp", "invoice_number", "vendor", "amount", "status",
         "approval_reasoning", "source_file"]
    ]


def flag_counts_df(df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Count of each validation flag type across all invoices, most common first."""
    df = pipeline_df() if df is None else df
    if df.empty:
        return pd.DataFrame(columns=["type", "count"])
    flat = [flag["type"] for flags in df["flags"] for flag in flags]
    if not flat:
        return pd.DataFrame(columns=["type", "count"])
    counts = pd.Series(flat).value_counts().reset_index()
    counts.columns = ["type", "count"]
    return counts


def vendor_breakdown_df(df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Per-vendor stats, reusing the same ledger lookup the approval agent reads from."""
    df = pipeline_df() if df is None else df
    if df.empty:
        return pd.DataFrame(
            columns=["vendor", "invoice_count", "paid_count", "rejected_count",
                     "avg_amount", "near_threshold_count"]
        )
    rows = []
    for vendor in sorted(df["vendor"].dropna().unique()):
        stats = ledger.get_vendor_stats(vendor)
        if stats is None:
            continue
        rows.append({"vendor": vendor, **stats})
    return pd.DataFrame(rows).sort_values("invoice_count", ascending=False).reset_index(drop=True)


def inventory_df() -> pd.DataFrame:
    """Current stock levels, straight from db/inventory.db."""
    conn = sqlite3.connect(ledger.DB_PATH)
    df = pd.read_sql_query("SELECT item, stock FROM inventory ORDER BY item", conn)
    conn.close()
    return df


# ---- Developer/ops telemetry (logs/telemetry.log) -----------------------
#
# Separate from pipeline_df() above: that's business-facing (what each
# invoice decided), this is engineering-facing (how the pipeline performed
# -- per-stage latency, failures, retries). Same underlying log-parsing
# trick (_read_json_stream) since telemetry.py writes in the same
# back-to-back-pretty-JSON format as pipeline.log/review.log.

def load_telemetry_entries() -> list[dict]:
    return _read_json_stream(TELEMETRY_LOG)


def run_telemetry_df() -> pd.DataFrame:
    """One row per invoice run: per-stage duration columns + outcome/failure info."""
    entries = [e for e in load_telemetry_entries() if e.get("kind") == "run"]
    if not entries:
        return pd.DataFrame(
            columns=["timestamp", "source_file", "invoice_number", "status",
                     "total_duration", "failed_stage", "error",
                     "ingestion", "validation", "approval", "payment"]
        )
    rows = []
    for e in entries:
        durations = e.get("stage_durations", {})
        rows.append(
            {
                "timestamp": e.get("timestamp"),
                "source_file": e.get("source_file"),
                "invoice_number": e.get("invoice_number"),
                "status": e.get("status"),
                "total_duration": e.get("total_duration"),
                "failed_stage": e.get("failed_stage"),
                "error": e.get("error"),
                "ingestion": durations.get("ingestion"),
                "validation": durations.get("validation"),
                "approval": durations.get("approval"),
                "payment": durations.get("payment"),
            }
        )
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp", ascending=False).reset_index(drop=True)


def retry_df() -> pd.DataFrame:
    """One row per retry event (e.g. ingestion's schema-validation retry)."""
    entries = [e for e in load_telemetry_entries() if e.get("kind") == "retry"]
    if not entries:
        return pd.DataFrame(columns=["timestamp", "source_file", "stage", "attempt"])
    df = pd.DataFrame(entries)[["timestamp", "source_file", "stage", "attempt"]]
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp", ascending=False).reset_index(drop=True)


def stage_latency_stats(run_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """avg/p50/p95/max latency per stage, in seconds, over successful runs."""
    run_df = run_telemetry_df() if run_df is None else run_df
    stages = ["ingestion", "validation", "approval", "payment"]
    if run_df.empty:
        return pd.DataFrame(columns=["stage", "avg", "p50", "p95", "max", "count"])
    rows = []
    for stage in stages:
        series = run_df[stage].dropna()
        if series.empty:
            continue
        rows.append(
            {
                "stage": stage,
                "avg": series.mean(),
                "p50": series.median(),
                "p95": series.quantile(0.95),
                "max": series.max(),
                "count": int(series.count()),
            }
        )
    return pd.DataFrame(rows)


def dev_kpis(run_df: Optional[pd.DataFrame] = None) -> dict:
    """Headline reliability/performance numbers for the dev dashboard's top tiles."""
    run_df = run_telemetry_df() if run_df is None else run_df
    if run_df.empty:
        return {"total_runs": 0, "success": 0, "failed": 0, "error_rate_pct": 0.0,
                "avg_total_duration": 0.0, "p95_total_duration": 0.0}
    total = len(run_df)
    failed = int((run_df["status"] == "failed").sum())
    success = total - failed
    return {
        "total_runs": total,
        "success": success,
        "failed": failed,
        "error_rate_pct": failed / total * 100 if total else 0.0,
        "avg_total_duration": float(run_df["total_duration"].mean()),
        "p95_total_duration": float(run_df["total_duration"].quantile(0.95)),
    }
