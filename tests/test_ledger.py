from agents import ledger


def test_is_duplicate_false_for_unknown_or_missing_invoice_number(test_db):
    assert ledger.is_duplicate("INV-999") is False
    assert ledger.is_duplicate(None) is False


def test_record_payment_then_is_duplicate_true(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    assert ledger.is_duplicate("INV-1") is True


def test_rejected_payment_is_not_a_duplicate(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="rejected",
    )
    assert ledger.is_duplicate("INV-1") is False


def test_get_paid_record_returns_source_and_amount(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    record = ledger.get_paid_record("INV-1")
    assert record == {"source_file": "a.txt", "amount": 100.0}


def test_get_paid_record_none_when_not_paid(test_db):
    assert ledger.get_paid_record("INV-1") is None
    assert ledger.get_paid_record(None) is None


def test_record_payment_upsert_same_invoice_and_source_overwrites(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="rejected",
    )
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    stats = ledger.get_vendor_stats("Acme")
    assert stats["invoice_count"] == 1
    assert stats["paid_count"] == 1
    assert stats["rejected_count"] == 0


def test_record_payment_different_source_file_creates_new_row(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    ledger.record_payment(
        invoice_number="INV-1", source_file="a_revised.txt", vendor="Acme",
        amount=150.0, invoice_date="2026-01-01", status="paid",
    )
    stats = ledger.get_vendor_stats("Acme")
    assert stats["invoice_count"] == 2


def test_get_vendor_stats_none_for_unknown_vendor(test_db):
    assert ledger.get_vendor_stats("Nobody") is None
    assert ledger.get_vendor_stats(None) is None


def test_is_structuring_pattern_requires_min_occurrences(test_db):
    # amounts land in the [8000, 10000) near-threshold band
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Acme",
        amount=9000.0, invoice_date="2026-01-01", status="paid",
    )
    assert ledger.is_structuring_pattern("Acme") is False

    ledger.record_payment(
        invoice_number="INV-2", source_file="b.txt", vendor="Acme",
        amount=9500.0, invoice_date="2026-01-02", status="paid",
    )
    assert ledger.is_structuring_pattern("Acme") is True


def test_is_structuring_pattern_false_for_unknown_vendor(test_db):
    assert ledger.is_structuring_pattern("Nobody") is False


def test_normalize_invoice_number_collapses_formatting_differences():
    assert ledger.normalize_invoice_number("INV-1012") == "INV-1012"
    assert ledger.normalize_invoice_number("INV 1012") == "INV-1012"
    assert ledger.normalize_invoice_number("inv_1012") == "INV-1012"
    assert ledger.normalize_invoice_number(None) is None
    assert ledger.normalize_invoice_number("") is None


def test_normalize_vendor_collapses_case_and_punctuation():
    assert ledger.normalize_vendor("Widgets Inc.") == "widgetsinc"
    assert ledger.normalize_vendor("WIDGETS INC") == "widgetsinc"
    assert ledger.normalize_vendor(None) is None


def test_is_duplicate_matches_across_invoice_number_formatting(test_db):
    ledger.record_payment(
        invoice_number="INV 1012", source_file="a.pdf", vendor="Acme",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    assert ledger.is_duplicate("INV-1012") is True
    assert ledger.is_duplicate("inv_1012") is True


def test_get_vendor_stats_matches_across_vendor_formatting(test_db):
    ledger.record_payment(
        invoice_number="INV-1", source_file="a.txt", vendor="Widgets Inc.",
        amount=100.0, invoice_date="2026-01-01", status="paid",
    )
    ledger.record_payment(
        invoice_number="INV-2", source_file="b.txt", vendor="WIDGETS INC",
        amount=200.0, invoice_date="2026-01-02", status="paid",
    )
    stats = ledger.get_vendor_stats("widgets inc")
    assert stats["invoice_count"] == 2
