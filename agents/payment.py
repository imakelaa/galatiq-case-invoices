"""Payment agent: executes the outcome of an approval decision.

Calls mock payment API if approved; otherwise logs to review.log (both
rejections and needs_review cases -- both are invoices that didn't get
paid and need a human to look at why).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from agents import ledger
from agents.approval import ApprovalResult
from agents.ingestion import InvoiceData
from agents.validation import ValidationResult

REVIEW_LOG = Path(__file__).parent.parent / "logs" / "review.log"
PIPELINE_LOG = Path(__file__).parent.parent / "logs" / "pipeline.log"


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


class PaymentResult(BaseModel):
    invoice_number: Optional[str]
    status: Literal["paid", "rejected", "needs_review"]
    detail: dict = {}


def _log_entry(invoice: InvoiceData, approval: ApprovalResult, status: str) -> None:
    REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "invoice_number": invoice.invoice_number,
        "source_file": invoice.source_file,
        "vendor": invoice.vendor,
        "amount": invoice.amount,
        "reasoning": approval.reasoning,
    }
    with REVIEW_LOG.open("a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")


def _log_pipeline_entry(
    invoice: InvoiceData, validation: ValidationResult, approval: ApprovalResult, result: PaymentResult
) -> None:
    """Append the full stage-by-stage record for this invoice, regardless of outcome.

    Unlike review.log (a queue of invoices needing human attention), this is
    the complete audit trail -- every invoice the pipeline touched, including
    paid ones, with what each stage saw and decided.
    """
    PIPELINE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "invoice": invoice.model_dump(),
        "validation": validation.model_dump(),
        "approval": approval.model_dump(),
        "payment": result.model_dump(),
    }
    with PIPELINE_LOG.open("a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")


def process_payment(invoice: InvoiceData, validation: ValidationResult, approval: ApprovalResult) -> PaymentResult:
    if approval.decision == "approve":
        response = mock_payment(invoice.vendor, invoice.amount)
        ledger.decrement_stock([(li.item, li.quantity) for li in invoice.items])
        result = PaymentResult(invoice_number=invoice.invoice_number, status="paid", detail=response)
    elif approval.decision == "needs_review":
        # Nothing settled yet -- don't touch the ledger (a paid/rejected row
        # here would let a later resubmission slip past is_duplicate, or
        # look like a resolved rejection, neither of which is true).
        _log_entry(invoice, approval, status="needs_review")
        result = PaymentResult(
            invoice_number=invoice.invoice_number,
            status="needs_review",
            detail={"reasoning": approval.reasoning, "logged_to": str(REVIEW_LOG)},
        )
        _log_pipeline_entry(invoice, validation, approval, result)
        return result
    else:
        _log_entry(invoice, approval, status="rejected")
        result = PaymentResult(
            invoice_number=invoice.invoice_number,
            status="rejected",
            detail={"reasoning": approval.reasoning, "logged_to": str(REVIEW_LOG)},
        )

    ledger.record_payment(
        invoice_number=invoice.invoice_number,
        source_file=invoice.source_file,
        vendor=invoice.vendor,
        amount=invoice.amount,
        # Prefer the normalized ISO date already computed by validation; fall back to the
        # raw extracted value if validation couldn't parse it (e.g. missing/fraudulent), so
        # we still keep something in the ledger rather than losing the field entirely.
        invoice_date=validation.invoice_date_iso or invoice.invoice_date,
        status=result.status,
    )
    _log_pipeline_entry(invoice, validation, approval, result)
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
    result = process_payment(invoice, validation, approval)
    print(json.dumps(result.model_dump(), indent=2))
