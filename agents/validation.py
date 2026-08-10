"""Validation agent: checks extracted invoice items against db/inventory.db.

Runs as a LangGraph ReAct agent. 

Grok looks at invoice items ingested by ingestion agent
and decides which item to look up via the query_inventory tool.

Grok is responsible for interpreting whether invoice fields are valid.
"""

import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from langchain_xai import ChatXAI
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from agents.ingestion import InvoiceData

DB_PATH = Path(__file__).parent.parent / "db" / "inventory.db"
MODEL = "grok-4-fast"

SYSTEM_PROMPT = """\
You are an invoice validation agent for an accounts payable system. You are
given a structured invoice extracted from a raw document. For every distinct
line item, call query_inventory to check it against the mock inventory
database, then flag any of the following:
  - unknown_item: the item does not exist in the inventory database
  - insufficient_stock: the requested quantity exceeds available stock
  - invalid_quantity: the requested quantity is zero or negative

Call query_inventory once per distinct item on the invoice before producing
your final answer. Do not skip any item. Items with no issues do not need a
flag. The invoice passes only if no flags were raised.

The summary must be derived from the flags list: only mention a problem in
the summary if there is a matching entry in flags, and every flag should be
reflected in the summary. Never state an issue in the summary that isn't
backed by a flag.
"""


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
    item: str = Field(description="The item this flag applies to")
    type: str = Field(description="unknown_item | insufficient_stock | invalid_quantity")
    message: str = Field(description="Human-readable explanation of the flag")


class ValidationResult(BaseModel):
    invoice_number: Optional[str] = Field(description="Invoice identifier this result applies to")
    passed: bool = Field(description="True only if no flags were raised")
    flags: list[Flag] = Field(default_factory=list)
    summary: str = Field(description="Short natural-language summary of the validation outcome")


def build_agent():
    llm = ChatXAI(model=MODEL)
    return create_react_agent(
        model=llm,
        tools=[query_inventory],
        prompt=SYSTEM_PROMPT,
        response_format=ValidationResult,
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


def validate_invoice(invoice: InvoiceData, verbose: bool = False) -> ValidationResult:
    agent = build_agent()
    input_state = {"messages": [{"role": "user", "content": invoice.model_dump_json(indent=2)}]}

    final_state = None
    for step in agent.stream(input_state, stream_mode="values"):
        final_state = step
        if verbose:
            _print_step(step)

    return final_state["structured_response"]


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
