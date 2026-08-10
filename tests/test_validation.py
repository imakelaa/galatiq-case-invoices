from datetime import date

from agents.validation import (
    AgentOutput,
    InventoryFlag,
    _build_validation_result,
    _implied_due_date,
    _normalize,
    _parse_iso,
)


def test_normalize_strips_case_whitespace_punctuation():
    assert _normalize("Widget A") == "widgeta"
    assert _normalize("  widget-a! ") == "widgeta"
    assert _normalize("WIDGET_A") == "widgeta"


def test_parse_iso_valid():
    assert _parse_iso("2026-01-15") == date(2026, 1, 15)


def test_parse_iso_invalid_or_missing():
    assert _parse_iso("not-a-date") is None
    assert _parse_iso(None) is None


def test_implied_due_date_net_days_multiple_of_30():
    assert _implied_due_date(date(2026, 1, 1), "net_days", 60) == date(2026, 3, 1)


def test_implied_due_date_net_days_not_multiple_of_30():
    assert _implied_due_date(date(2026, 1, 1), "net_days", 15) == date(2026, 1, 16)


def test_implied_due_date_end_of_month():
    assert _implied_due_date(date(2026, 2, 1), "end_of_month", None) == date(2026, 2, 28)


def test_implied_due_date_immediate():
    assert _implied_due_date(date(2026, 1, 1), "immediate", None) == date(2026, 1, 1)


def test_implied_due_date_unrecognized_or_missing_terms():
    assert _implied_due_date(date(2026, 1, 1), "unrecognized", None) is None
    assert _implied_due_date(date(2026, 1, 1), None, None) is None
    assert _implied_due_date(date(2026, 1, 1), "net_days", None) is None


def _base_output(**overrides) -> AgentOutput:
    defaults = dict(
        invoice_number="INV-1",
        inventory_flags=[],
        invoice_date_iso="2026-01-01",
        invoice_date_issue=None,
        due_date_iso=None,
        due_date_issue=None,
        payment_terms_type=None,
        payment_terms_days=None,
    )
    defaults.update(overrides)
    return AgentOutput(**defaults)


def test_missing_invoice_date_blocks_and_clears_due_date():
    result = _build_validation_result(_base_output(invoice_date_iso=None, invoice_date_issue="missing"))
    assert result.passed is False
    assert result.effective_due_date is None
    assert [f.type for f in result.flags] == ["missing_invoice_date"]
    assert result.flags[0].blocking is True


def test_fraudulent_invoice_date_blocks():
    output = _base_output(invoice_date_iso=None, invoice_date_issue="fraudulent", due_date_iso="2026-02-01")
    result = _build_validation_result(output)
    flag_types = [f.type for f in result.flags]
    assert "unparseable_date" in flag_types
    assert result.passed is False
    # due_date is still stated explicitly, so it's used even though invoice_date is unusable
    assert result.effective_due_date == "2026-02-01"


def test_fraudulent_due_date_blocks_and_clears_effective_due_date():
    output = _base_output(due_date_iso=None, due_date_issue="fraudulent")
    result = _build_validation_result(output)
    assert result.passed is False
    assert result.effective_due_date is None
    assert any(f.type == "unparseable_date" and f.item == "due_date" for f in result.flags)


def test_due_date_matches_implied_no_mismatch_flag():
    output = _base_output(
        due_date_iso="2026-02-01",
        payment_terms_type="net_days",
        payment_terms_days=30,
    )
    result = _build_validation_result(output)
    assert result.passed is True
    assert result.flags == []
    assert result.effective_due_date == "2026-02-01"


def test_due_date_mismatch_is_nonblocking_but_flagged():
    output = _base_output(
        due_date_iso="2026-02-05",
        payment_terms_type="net_days",
        payment_terms_days=30,
    )
    result = _build_validation_result(output)
    assert result.passed is True  # non-blocking
    assert len(result.flags) == 1
    assert result.flags[0].type == "due_date_mismatch"
    assert result.flags[0].category == "discrepant"
    assert result.flags[0].blocking is False
    # stated due_date wins even though it disagrees with the calculated one
    assert result.effective_due_date == "2026-02-05"


def test_due_date_absent_uses_implied_due_date_with_no_flag():
    output = _base_output(payment_terms_type="net_days", payment_terms_days=15)
    result = _build_validation_result(output)
    assert result.passed is True
    assert result.flags == []
    assert result.effective_due_date == "2026-01-16"


def test_due_date_and_terms_absent_defaults_to_30_days_with_warning():
    result = _build_validation_result(_base_output())
    assert result.passed is True  # missing_due_date is non-blocking
    assert len(result.flags) == 1
    assert result.flags[0].type == "missing_due_date"
    assert result.flags[0].blocking is False
    assert result.effective_due_date == "2026-01-31"


def test_inventory_flags_pass_through_as_blocking():
    output = _base_output(
        inventory_flags=[InventoryFlag(item="WidgetA", type="insufficient_stock", message="20 > 15")],
        due_date_iso="2026-01-31",
        payment_terms_type="net_days",
        payment_terms_days=30,
    )
    result = _build_validation_result(output)
    assert result.passed is False
    assert result.flags[0].type == "insufficient_stock"
    assert result.flags[0].blocking is True
