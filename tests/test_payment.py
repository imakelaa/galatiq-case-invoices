import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from agents import approval as approval_module
from agents import ledger, payment
from agents.approval import ApprovalResult, CritiqueVerdict, Proposal, approve_invoice
from agents.ingestion import InvoiceData, LineItem
from agents.validation import ValidationResult


@pytest.fixture(autouse=True)
def temp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(payment, "REVIEW_LOG", tmp_path / "review.log")
    monkeypatch.setattr(payment, "PIPELINE_LOG", tmp_path / "pipeline.log")


def _invoice(**overrides) -> InvoiceData:
    defaults = dict(
        invoice_number="INV-1",
        vendor="Acme",
        amount=100.0,
        invoice_date="2026-01-01",
        due_date=None,
        payment_terms=None,
        items=[],
        source_file="a.txt",
    )
    defaults.update(overrides)
    return InvoiceData(**defaults)


def _approval(decision, **overrides) -> ApprovalResult:
    defaults = dict(invoice_number="INV-1", decision=decision, reasoning="because", critique_rounds=0)
    defaults.update(overrides)
    return ApprovalResult(**defaults)


def _validation(**overrides) -> ValidationResult:
    defaults = dict(
        invoice_number="INV-1", passed=True, flags=[], summary="ok",
        effective_due_date=None, invoice_date_iso="2026-01-01",
    )
    defaults.update(overrides)
    return ValidationResult(**defaults)


def test_log_entry_writes_indented_readable_json(tmp_path):
    log_path = tmp_path / "out.log"
    payment._log_entry(log_path, _invoice(), _approval("reject"))

    content = log_path.read_text()
    assert "\n" in content.strip()  # spread across multiple lines, not one blob
    entry = json.loads(content)
    assert entry["invoice_number"] == "INV-1"
    assert entry["vendor"] == "Acme"
    assert entry["reasoning"] == "because"


def test_process_payment_approve_pays_and_records_ledger(test_db):
    result = payment.process_payment(_invoice(), _validation(), _approval("approve"))
    assert result.status == "paid"
    assert ledger.is_duplicate("INV-1") is True


def test_process_payment_reject_logs_and_records_ledger(test_db):
    result = payment.process_payment(_invoice(), _validation(), _approval("reject"))
    assert result.status == "rejected"
    assert payment.REJECTIONS_LOG.exists()
    assert ledger.is_duplicate("INV-1") is False  # rejected, not paid
    assert ledger.get_vendor_stats("Acme")["rejected_count"] == 1


def test_process_payment_needs_review_does_not_touch_ledger(test_db):
    result = payment.process_payment(_invoice(), _validation(), _approval("needs_review"))
    assert result.status == "needs_review"
    assert payment.MANUAL_REVIEW_LOG.exists()
    assert ledger.get_vendor_stats("Acme") is None


@pytest.mark.parametrize("decision,status", [("approve", "paid"), ("reject", "rejected"), ("needs_review", "needs_review")])
def test_process_payment_always_writes_pipeline_log(test_db, decision, status):
    payment.process_payment(_invoice(), _validation(), _approval(decision))
    assert payment.PIPELINE_LOG.exists()
    entry = json.loads(payment.PIPELINE_LOG.read_text())
    assert entry["invoice"]["invoice_number"] == "INV-1"
    assert entry["validation"]["invoice_date_iso"] == "2026-01-01"
    assert entry["approval"]["decision"] == decision
    assert entry["payment"]["status"] == status


def test_process_payment_uses_normalized_invoice_date_from_validation(test_db):
    payment.process_payment(
        _invoice(invoice_date="Jan 1, 2026"), _validation(invoice_date_iso="2026-01-01"), _approval("approve")
    )
    conn = sqlite3.connect(test_db)
    row = conn.execute("SELECT invoice_date FROM payments WHERE invoice_number = 'INV-1'").fetchone()
    conn.close()
    assert row[0] == "2026-01-01"


class _FakeStructuredLLM:
    """Stands in for `ChatXAI(...).with_structured_output(schema)` -- returns a
    fixed response regardless of the prompt, so approve_invoice's propose/critique
    loop can run without hitting the real xAI API."""

    def __init__(self, response):
        self._response = response

    def invoke(self, messages):
        return self._response


class _FakeChatXAI:
    def __init__(self, model=None):
        pass

    def with_structured_output(self, schema):
        if schema is Proposal:
            return _FakeStructuredLLM(Proposal(decision="approve", reasoning="looks fine"))
        if schema is CritiqueVerdict:
            return _FakeStructuredLLM(CritiqueVerdict(verdict="confirm", feedback="agreed"))
        raise AssertionError(f"unexpected schema {schema}")


def test_rerun_of_paid_invoice_does_not_double_pay_or_double_deduct(test_db, monkeypatch):
    monkeypatch.setattr(approval_module, "ChatXAI", _FakeChatXAI)

    mock_payment_spy = MagicMock(wraps=payment.mock_payment)
    monkeypatch.setattr(payment, "mock_payment", mock_payment_spy)

    decrement_spy = MagicMock(wraps=ledger.decrement_stock)
    monkeypatch.setattr(ledger, "decrement_stock", decrement_spy)

    invoice = _invoice(items=[LineItem(item="WidgetA", quantity=2)])
    validation = _validation()

    # First run: nothing paid yet -> LLM loop runs, approves, pays, deducts stock.
    approval = approve_invoice(invoice, validation)
    assert approval.decision == "approve"
    result = payment.process_payment(invoice, validation, approval)
    assert result.status == "paid"

    # Second run of the exact same invoice: get_paid_record short-circuits
    # approve_invoice to needs_review before the LLM loop even runs, and
    # process_payment's needs_review branch returns before mock_payment /
    # decrement_stock / record_payment.
    approval_again = approve_invoice(invoice, validation)
    assert approval_again.decision == "needs_review"
    result_again = payment.process_payment(invoice, validation, approval_again)
    assert result_again.status == "needs_review"

    assert mock_payment_spy.call_count == 1
    assert decrement_spy.call_count == 1

    conn = sqlite3.connect(test_db)
    stock = conn.execute("SELECT stock FROM inventory WHERE item = 'WidgetA'").fetchone()[0]
    conn.close()
    assert stock == 15 - 2  # deducted exactly once, not twice
