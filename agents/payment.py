"""Payment agent: executes the outcome of an approval decision.

Calls mock payment API (if approved in validation stage; 
else logs reason for rejection to rejections.log)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from agents import ledger
from agents.approval import ApprovalResult
from agents.ingestion import InvoiceData

REJECTIONS_LOG = Path(__file__).parent.parent / "logs" / "rejections.log"
MANUAL_REVIEW_LOG = Path(__file__).parent.parent / "logs" / "manual_review.log"


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


class PaymentResult(BaseModel):
    invoice_number: Optional[str]
    status: Literal["paid", "rejected", "needs_review"]
    detail: dict = {}


def _log_entry(log_path: Path, invoice: InvoiceData, approval: ApprovalResult) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "invoice_number": invoice.invoice_number,
        "source_file": invoice.source_file,
        "vendor": invoice.vendor,
        "amount": invoice.amount,
        "reasoning": approval.reasoning,
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def process_payment(invoice: InvoiceData, approval: ApprovalResult) -> PaymentResult:
    if approval.decision == "approve":
        response = mock_payment(invoice.vendor, invoice.amount)
        result = PaymentResult(invoice_number=invoice.invoice_number, status="paid", detail=response)
    elif approval.decision == "needs_review":
        # Nothing settled yet -- don't touch the ledger (a paid/rejected row
        # here would let a later resubmission slip past is_duplicate, or
        # look like a resolved rejection, neither of which is true).
        _log_entry(MANUAL_REVIEW_LOG, invoice, approval)
        return PaymentResult(
            invoice_number=invoice.invoice_number,
            status="needs_review",
            detail={"reasoning": approval.reasoning, "logged_to": str(MANUAL_REVIEW_LOG)},
        )
    else:
        _log_entry(REJECTIONS_LOG, invoice, approval)
        result = PaymentResult(
            invoice_number=invoice.invoice_number,
            status="rejected",
            detail={"reasoning": approval.reasoning, "logged_to": str(REJECTIONS_LOG)},
        )

    ledger.record_payment(
        invoice_number=invoice.invoice_number,
        source_file=invoice.source_file,
        vendor=invoice.vendor,
        amount=invoice.amount,
        invoice_date=invoice.invoice_date,
        status=result.status,
    )
    return result


if __name__ == "__main__":
    import argparse

    from agents.approval import approve_invoice
    from agents.ingestion import extract_invoice
    from agents.validation import validate_invoice

    parser = argparse.ArgumentParser(description="Run payment on an invoice")
    parser.add_argument("--invoice_path", required=True, type=Path)
    args = parser.parse_args()

    invoice = extract_invoice(args.invoice_path)
    validation = validate_invoice(invoice)
    approval = approve_invoice(invoice, validation)
    result = process_payment(invoice, approval)
    print(json.dumps(result.model_dump(), indent=2))
