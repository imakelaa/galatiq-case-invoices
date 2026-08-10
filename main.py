"""Entry point for the invoice processing pipeline.

Usage:
    python3 main.py --invoice_path=<path-to-invoice-file>

    (optional) --verbose: stream node-by-node LangGraph execution to stderr as it runs

Runs the full pipeline against a single invoice file and prints structured
results for each stage: ingestion -> validation -> approval -> payment.
"""

import argparse
import json
from pathlib import Path

from agents.approval import approve_invoice
from agents.ingestion import extract_invoice
from agents.payment import process_payment
from agents.validation import validate_invoice


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the invoice processing pipeline on a single invoice")
    parser.add_argument("--invoice_path", required=True, type=Path)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream node-by-node LangGraph execution to stderr as it runs",
    )
    args = parser.parse_args()

    print(f"[ingestion] extracting {args.invoice_path.name}...")
    invoice = extract_invoice(args.invoice_path)
    print(json.dumps(invoice.model_dump(), indent=2))

    print(f"\n[validation] checking {invoice.invoice_number} against inventory...")
    validation = validate_invoice(invoice, verbose=args.verbose)
    print(json.dumps(validation.model_dump(), indent=2))

    print(f"\n[approval] reviewing {invoice.invoice_number}...")
    approval = approve_invoice(invoice, validation, verbose=args.verbose)
    print(json.dumps(approval.model_dump(), indent=2))

    print(f"\n[payment] processing {invoice.invoice_number}...")
    payment = process_payment(invoice, approval)
    print(json.dumps(payment.model_dump(), indent=2))


if __name__ == "__main__":
    main()
