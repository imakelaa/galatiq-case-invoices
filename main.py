"""Entry point for the invoice processing pipeline.

Usage:
    python3 main.py --invoice_path=<path-to-invoice-file>
    python3 main.py --invoice_dir=<path-to-directory>

    (optional) --verbose: stream node-by-node LangGraph execution to stderr as it runs

CURRENTLY: Runs the full pipeline against one invoice, or every supported invoice file
in a directory, and prints structured results for each stage: ingestion ->
validation -> approval -> payment. In directory mode, a failure on one
invoice is logged and the rest of the batch still runs.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.approval import approve_invoice
from agents.ingestion import discover_invoice_files, extract_invoice
from agents.payment import process_payment
from agents.validation import validate_invoice

# Concurrent invoices in batch mode (--invoice_dir). Kept modest rather than
# firing the whole batch at once: the pipeline is dominated by xAI API calls,
# and higher concurrency risks tripping account-level rate limits (which can
# surface as the model silently skipping its structured-output call rather
# than a clean 429).
BATCH_WORKERS = 3


def run_pipeline(invoice_path: Path, verbose: bool = False) -> str:
    """Runs the full pipeline for one invoice and returns its report as a string.

    Builds the report in a local list rather than printing directly, so
    concurrent worker threads (see run_batch) never touch shared stdout
    state -- each thread's output is self-contained until the caller prints
    it as one block.
    """
    lines: list[str] = []

    lines.append(f"[ingestion] extracting {invoice_path.name}...")
    invoice = extract_invoice(invoice_path)
    lines.append(json.dumps(invoice.model_dump(), indent=2))

    lines.append(f"\n[validation] checking {invoice.invoice_number} against inventory...")
    validation = validate_invoice(invoice, verbose=verbose)
    lines.append(json.dumps(validation.model_dump(), indent=2))

    lines.append(f"\n[approval] reviewing {invoice.invoice_number}...")
    approval = approve_invoice(invoice, validation, verbose=verbose)
    lines.append(json.dumps(approval.model_dump(), indent=2))

    lines.append(f"\n[payment] processing {invoice.invoice_number}...")
    payment = process_payment(invoice, validation, approval)
    lines.append(json.dumps(payment.model_dump(), indent=2))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the invoice processing pipeline")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--invoice_path", type=Path, help="Path to a single invoice file")
    target.add_argument("--invoice_dir", type=Path, help="Directory of invoice files to process as a batch")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream node-by-node LangGraph execution to stderr as it runs",
    )
    args = parser.parse_args()

    if args.invoice_path:
        print(run_pipeline(args.invoice_path, verbose=args.verbose))
        return

    files = discover_invoice_files(args.invoice_dir)
    failures: list[tuple[Path, Exception]] = []
    first = True
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as executor:
        future_to_path = {
            executor.submit(run_pipeline, path, args.verbose): path for path in files
        }
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            if not first:
                print("\n" + "=" * 80 + "\n")
            first = False
            try:
                print(future.result())
            except Exception as exc:
                failures.append((path, exc))
                print(f"[FAILED] {path.name}: {exc}", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} of {len(files)} invoice(s) failed:", file=sys.stderr)
        for path, exc in failures:
            print(f"  {path.name}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
