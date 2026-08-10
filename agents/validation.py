"""Validation agent: checks extracted invoice items against db/inventory.db.

Runs as a LangGraph ReAct agent.

Grok looks at invoice items ingested by ingestion agent and looks them 
up via the query_inventory tool.

Deterministically computed by python:
    1. date arithmetic (effective_due_date, passed, summary)
    2. flag construction (blocking, category, message)
"""

import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

from dateutil.relativedelta import relativedelta
from langchain_core.tools import tool
from langchain_xai import ChatXAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from agents.ingestion import InvoiceData
from prompts import load_prompt

DB_PATH = Path(__file__).parent.parent / "db" / "inventory.db"
MODEL = "grok-4-fast"

SYSTEM_PROMPT = load_prompt("validation_system")


def _normalize(name: str) -> str:
    """
    Normalize differences in case, whitespace, and punctuation by lowercasing and stripping.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


@tool
def query_inventory(item: str) -> dict:
    """
    Look up an item in the mock inventory database.

    Matches normalized results.

    Returns {"item": item, "found": bool, "stock": int | None}.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item, stock FROM inventory").fetchall()
    conn.close()

    target = _normalize(item)
    for db_item, stock in rows:
        if _normalize(db_item) == target:
            return {"item": item, "found": True, "stock": stock}
    return {"item": item, "found": False, "stock": None}


class Flag(BaseModel):
    item: str = Field(
        description="The item or field this flag applies to (e.g. an item name, or 'due_date'/'invoice_date' for date flags)"
    )
    type: str = Field(
        description=(
            "unknown_item | insufficient_stock | invalid_quantity | due_date_mismatch | "
            "unparseable_date | missing_due_date | missing_invoice_date"
        )
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "For date-related flags only: fraudulent (vague/relative/nonsensical date), "
            "discrepant (due_date disagrees with invoice_date + payment_terms), "
            "missing (due_date and payment_terms both absent, 30-day default applied), or "
            "invalid (invoice_date itself is absent -- invoice cannot be paid). Null for "
            "the inventory flags (unknown_item, insufficient_stock, invalid_quantity)."
        ),
    )
    message: str = Field(description="Human-readable explanation of the flag")
    blocking: bool = Field(
        description=(
            "Whether this flag alone should block payment. True for inventory flags, "
            "unparseable_date (fraudulent), and missing_invoice_date (invalid). False for "
            "due_date_mismatch (discrepant) and missing_due_date (missing) -- these are "
            "informational warnings, not reasons to withhold payment on their own."
        )
    )


class ValidationResult(BaseModel):
    invoice_number: Optional[str] = Field(description="Invoice identifier this result applies to")
    passed: bool = Field(description="True unless a blocking flag was raised (non-blocking warnings don't fail it)")
    flags: list[Flag] = Field(default_factory=list)
    summary: str = Field(description="Short natural-language summary of the validation outcome")
    effective_due_date: Optional[str] = Field(
        description=(
            "The due date to use downstream: the stated due_date if present (even if flagged "
            "as a mismatch -- the flag surfaces the discrepancy, it doesn't override the stated "
            "date), otherwise one calculated from invoice_date + payment_terms, otherwise null"
        )
    )
    invoice_date_iso: Optional[str] = Field(
        description=(
            "invoice_date normalized to ISO YYYY-MM-DD (the same parsing already done to compute "
            "effective_due_date), for downstream storage -- null if invoice_date was missing or "
            "unparseable/fraudulent"
        )
    )


class InventoryFlag(BaseModel):
    item: str = Field(description="The line item this flag applies to")
    type: str = Field(description="unknown_item | insufficient_stock | invalid_quantity")
    message: str = Field(description="Human-readable explanation of the flag")


class AgentOutput(BaseModel):
    invoice_number: Optional[str] = Field(description="Invoice identifier this result applies to")
    inventory_flags: list[InventoryFlag] = Field(default_factory=list)
    invoice_date_iso: Optional[str] = Field(description="invoice_date as ISO YYYY-MM-DD, or null")
    invoice_date_issue: Optional[Literal["missing", "fraudulent"]] = Field(
        default=None, description="'missing' if invoice_date wasn't stated; 'fraudulent' if vague/relative/corrupted"
    )
    due_date_iso: Optional[str] = Field(description="due_date as ISO YYYY-MM-DD, or null")
    due_date_issue: Optional[Literal["missing", "fraudulent"]] = Field(
        default=None, description="'missing' if due_date wasn't stated; 'fraudulent' if vague/relative/corrupted"
    )
    payment_terms_type: Optional[Literal["net_days", "end_of_month", "immediate", "unrecognized"]] = Field(
        default=None, description="Semantic type of payment_terms"
    )
    payment_terms_days: Optional[int] = Field(
        default=None, description="The N in 'Net N'; only set when payment_terms_type is 'net_days'"
    )


def _parse_iso(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _implied_due_date(
    invoice_date: date, terms_type: Optional[str], terms_days: Optional[int]
) -> Optional[date]:
    if terms_type == "net_days" and terms_days:
        if terms_days % 30 == 0:
            return invoice_date + relativedelta(months=terms_days // 30)
        return invoice_date + timedelta(days=terms_days)
    if terms_type == "end_of_month":
        return invoice_date + relativedelta(day=31)
    if terms_type == "immediate":
        return invoice_date
    return None


def _finalize(
    invoice_number: Optional[str],
    flags: list[Flag],
    effective_due_date: Optional[date],
    invoice_date_iso: Optional[str],
) -> ValidationResult:
    return ValidationResult(
        invoice_number=invoice_number,
        passed=not any(f.blocking for f in flags),
        flags=flags,
        summary=" ".join(f.message for f in flags) if flags else "Invoice passes validation with no issues.",
        effective_due_date=effective_due_date.isoformat() if effective_due_date else None,
        invoice_date_iso=invoice_date_iso,
    )


def _build_validation_result(agent_output: AgentOutput) -> ValidationResult:
    flags: list[Flag] = [
        Flag(item=f.item, type=f.type, category=None, message=f.message, blocking=True)
        for f in agent_output.inventory_flags
    ]

    invoice_date = _parse_iso(agent_output.invoice_date_iso)
    due_date = _parse_iso(agent_output.due_date_iso)
    # Captured once, before invoice_date may get cleared below in the fraudulent branch --
    # this is the normalized value to hand downstream regardless of what happens to `invoice_date`.
    invoice_date_iso = invoice_date.isoformat() if invoice_date else None

    if agent_output.invoice_date_issue == "missing" or (
        invoice_date is None and agent_output.invoice_date_issue != "fraudulent"
    ):
        flags.append(
            Flag(
                item="invoice_date",
                type="missing_invoice_date",
                category="invalid",
                message="invoice_date is missing entirely; an invoice with no invoice_date cannot be paid.",
                blocking=True,
            )
        )
        return _finalize(agent_output.invoice_number, flags, None, invoice_date_iso)

    if agent_output.invoice_date_issue == "fraudulent":
        flags.append(
            Flag(
                item="invoice_date",
                type="unparseable_date",
                category="fraudulent",
                message=(
                    "invoice_date is vague, relative, or corrupted beyond confident reconstruction "
                    "-- a common fraud/urgency tactic."
                ),
                blocking=True,
            )
        )
        invoice_date = None  # unusable as an anchor for any calculation below

    implied_due_date = (
        _implied_due_date(invoice_date, agent_output.payment_terms_type, agent_output.payment_terms_days)
        if invoice_date is not None
        else None
    )

    if agent_output.due_date_issue == "fraudulent":
        flags.append(
            Flag(
                item="due_date",
                type="unparseable_date",
                category="fraudulent",
                message=(
                    "due_date is vague, relative, or corrupted beyond confident reconstruction "
                    "-- a common fraud/urgency tactic."
                ),
                blocking=True,
            )
        )
        return _finalize(agent_output.invoice_number, flags, None, invoice_date_iso)

    if due_date is not None:
        if implied_due_date is not None and implied_due_date != due_date:
            flags.append(
                Flag(
                    item="due_date",
                    type="due_date_mismatch",
                    category="discrepant",
                    message=(
                        f"Calculated due date from invoice_date + payment terms is "
                        f"{implied_due_date.isoformat()}; stated due_date is {due_date.isoformat()}."
                    ),
                    blocking=False,
                )
            )
        return _finalize(agent_output.invoice_number, flags, due_date, invoice_date_iso)

    if implied_due_date is not None:
        return _finalize(agent_output.invoice_number, flags, implied_due_date, invoice_date_iso)

    if invoice_date is not None:
        fallback = invoice_date + timedelta(days=30)
        flags.append(
            Flag(
                item="due_date",
                type="missing_due_date",
                category="missing",
                message=(
                    "due_date and payment_terms are both absent or unusable; defaulted "
                    "effective_due_date to invoice_date + 30 days."
                ),
                blocking=False,
            )
        )
        return _finalize(agent_output.invoice_number, flags, fallback, invoice_date_iso)

    return _finalize(agent_output.invoice_number, flags, None, invoice_date_iso)


def build_agent():
    llm = ChatXAI(model=MODEL)
    return create_react_agent(
        model=llm,
        tools=[query_inventory],
        prompt=SYSTEM_PROMPT,
        response_format=AgentOutput,
    )


def _print_step(step: dict) -> None:
    messages = step.get("messages", [])
    if not messages:
        return
    last = messages[-1]
    role = getattr(last, "type", "?")
    content = getattr(last, "content", "")
    print(f"[validation] {role}: {content}", file=sys.stderr)
    for call in getattr(last, "tool_calls", None) or []:
        print(f"[validation]   tool_call -> {call['name']}({call['args']})", file=sys.stderr)


MAX_VALIDATION_ATTEMPTS = 2  # initial attempt + 1 retry if the model ends a turn without the structured call


def validate_invoice(invoice: InvoiceData, verbose: bool = False) -> ValidationResult:
    agent = build_agent()
    input_state = {"messages": [{"role": "user", "content": invoice.model_dump_json(indent=2)}]}

    agent_output: Optional[AgentOutput] = None
    for attempt in range(MAX_VALIDATION_ATTEMPTS):
        final_state = None
        for step in agent.stream(input_state, stream_mode="values"):
            final_state = step
            if verbose:
                _print_step(step)

        agent_output = final_state["structured_response"]
        if agent_output is not None:
            break
        if verbose:
            print(
                "[validation] model ended its turn without the structured output call; retrying",
                file=sys.stderr,
            )

    if agent_output is None:
        raise RuntimeError(
            f"Validation agent for {invoice.invoice_number!r} ended its turn without producing "
            f"a structured response after {MAX_VALIDATION_ATTEMPTS} attempt(s) -- the model likely "
            "replied with plain text instead of calling the final schema."
        )

    return _build_validation_result(agent_output)


if __name__ == "__main__":
    import argparse
    import json

    from agents.ingestion import extract_invoice

    parser = argparse.ArgumentParser(description="Validate an invoice against the mock inventory DB")
    parser.add_argument("--invoice_path", required=True, type=Path)
    args = parser.parse_args()

    invoice = extract_invoice(args.invoice_path)
    result = validate_invoice(invoice)
    print(json.dumps(result.model_dump(), indent=2))
