"""Telemetry: per-stage latency, failures, and retries -- the data the
developer dashboard needs that logs/pipeline.log doesn't carry.

pipeline.log records what each stage *decided*; this records how the
pipeline *performed* (timing, errors, retries). Written by main.py (stage
timing/failures) and agents/ingestion.py (retry events). Read-only from
observability/data.py's perspective.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TELEMETRY_LOG = Path(__file__).parent.parent / "logs" / "telemetry.log"


def _write(entry: dict) -> None:
    TELEMETRY_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    with TELEMETRY_LOG.open("a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")


def log_run(
    source_file: str,
    stage_durations: dict[str, float],
    total_duration: float,
    status: str,
    invoice_number: Optional[str] = None,
    failed_stage: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """One entry per invoice run: how long each stage took, and where/why it broke, if it did."""
    _write(
        {
            "kind": "run",
            "source_file": source_file,
            "invoice_number": invoice_number,
            "status": status,
            "stage_durations": stage_durations,
            "total_duration": total_duration,
            "failed_stage": failed_stage,
            "error": error,
        }
    )


def log_retry(source_file: str, stage: str, attempt: int) -> None:
    """One entry per retry a stage needed (e.g. ingestion's schema-validation retry)."""
    _write({"kind": "retry", "source_file": source_file, "stage": stage, "attempt": attempt})


if __name__ == "__main__":
    log_run(
        source_file="demo.txt", stage_durations={"ingestion": 1.2}, total_duration=1.2,
        status="success", invoice_number="INV-DEMO",
    )
