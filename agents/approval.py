"""Approval agent: simulates VP-level review with a propose/critique loop.

Approval agent relies on results of the validation agent.
If invoice has been validated, agent reasons through an approve/reject decision.

CURRENTLY: agent does 2 revisions maximum before finalizing decision.
"""

import sys
from pathlib import Path
from typing import Literal, Optional, TypedDict

from langchain_xai import ChatXAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agents.ingestion import InvoiceData
from agents.validation import ValidationResult

MODEL = "grok-4-fast"
MAX_REVISIONS = 2

PROPOSE_SYSTEM_PROMPT = """\
You are a VP-level approver in an accounts payable system, deciding whether
to approve or reject an invoice for payment. You are given the extracted
invoice data and its inventory validation result.

Rules to apply:
  - Invoices over $10,000 require additional scrutiny -- reason explicitly
    about vendor legitimacy, amount reasonableness, and validation flags
    before deciding, and lean toward rejection if anything looks off.
  - Any validation flag (unknown item, insufficient stock, invalid
    quantity) is a serious concern and should usually result in rejection
    unless you have a well-justified reason to approve anyway.
  - Reject anything that looks fraudulent (e.g. suspicious urgency,
    missing/garbled vendor info, implausible amounts).

If you are given feedback from a previous critique, address it directly and
revise your reasoning accordingly.
"""

CRITIQUE_SYSTEM_PROMPT = """\
You are a skeptical compliance reviewer double-checking a VP's
approve/reject decision on an invoice before it is finalized. You are given
the invoice, its validation result, and the proposed decision with
reasoning. Look for: rules applied incorrectly, validation flags that were
ignored or hand-waved, weak justification for approving a large or
suspicious invoice, or reasoning that doesn't actually support the stated
decision.

If the reasoning is sound, confirm it. Otherwise, send it back for revision
with specific, actionable feedback on what's wrong.
"""


class Proposal(BaseModel):
    decision: Literal["approve", "reject"]
    reasoning: str = Field(description="Explanation for the decision, addressing the rules and any validation flags")


class CritiqueVerdict(BaseModel):
    verdict: Literal["confirm", "revise"]
    feedback: str = Field(description="Why confirmed, or what specifically needs to change if revising")


class ApprovalResult(BaseModel):
    invoice_number: Optional[str]
    decision: Literal["approve", "reject"]
    reasoning: str
    critique_rounds: int = Field(description="How many propose/critique cycles ran before finalizing")


class ApprovalState(TypedDict):
    invoice: InvoiceData
    validation: ValidationResult
    proposal: Optional[Proposal]
    critique: Optional[CritiqueVerdict]
    revision_count: int


def _context_block(state: ApprovalState) -> str:
    return (
        f"Invoice:\n{state['invoice'].model_dump_json(indent=2)}\n\n"
        f"Validation result:\n{state['validation'].model_dump_json(indent=2)}"
    )


def propose_node(state: ApprovalState) -> dict:
    llm = ChatXAI(model=MODEL).with_structured_output(Proposal)

    content = _context_block(state)
    if state.get("critique"):
        content += (
            f"\n\nYour previous proposal:\n{state['proposal'].model_dump_json(indent=2)}\n\n"
            f"Critique feedback to address:\n{state['critique'].feedback}"
        )

    proposal = llm.invoke(
        [{"role": "system", "content": PROPOSE_SYSTEM_PROMPT}, {"role": "user", "content": content}]
    )
    return {"proposal": proposal}


def critique_node(state: ApprovalState) -> dict:
    llm = ChatXAI(model=MODEL).with_structured_output(CritiqueVerdict)

    content = f"{_context_block(state)}\n\nProposed decision:\n{state['proposal'].model_dump_json(indent=2)}"

    verdict = llm.invoke(
        [{"role": "system", "content": CRITIQUE_SYSTEM_PROMPT}, {"role": "user", "content": content}]
    )
    revision_count = state["revision_count"] + (1 if verdict.verdict == "revise" else 0)
    return {"critique": verdict, "revision_count": revision_count}


def route_after_critique(state: ApprovalState) -> str:
    if state["critique"].verdict == "revise" and state["revision_count"] < MAX_REVISIONS:
        return "propose"
    return END


def build_graph():
    graph = StateGraph(ApprovalState)
    graph.add_node("propose", propose_node)
    graph.add_node("critique", critique_node)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "critique")
    graph.add_conditional_edges("critique", route_after_critique, {"propose": "propose", END: END})
    return graph.compile()


def _print_node(node_name: str, partial: dict) -> None:
    if "proposal" in partial:
        p = partial["proposal"]
        print(f"[approval] {node_name} -> decision={p.decision}", file=sys.stderr)
        print(f"[approval]   reasoning: {p.reasoning}", file=sys.stderr)
    if "critique" in partial:
        c = partial["critique"]
        print(
            f"[approval] {node_name} -> verdict={c.verdict} "
            f"(revision_count={partial.get('revision_count')})",
            file=sys.stderr,
        )
        print(f"[approval]   feedback: {c.feedback}", file=sys.stderr)


def approve_invoice(invoice: InvoiceData, validation: ValidationResult, verbose: bool = False) -> ApprovalResult:
    app = build_graph()
    initial_state = {
        "invoice": invoice,
        "validation": validation,
        "proposal": None,
        "critique": None,
        "revision_count": 0,
    }

    final_state = dict(initial_state)
    for update in app.stream(initial_state, stream_mode="updates"):
        for node_name, partial in update.items():
            final_state.update(partial)
            if verbose:
                _print_node(node_name, partial)

    return ApprovalResult(
        invoice_number=invoice.invoice_number,
        decision=final_state["proposal"].decision,
        reasoning=final_state["proposal"].reasoning,
        critique_rounds=final_state["revision_count"],
    )


if __name__ == "__main__":
    import argparse
    import json

    from agents.ingestion import extract_invoice
    from agents.validation import validate_invoice

    parser = argparse.ArgumentParser(description="Run approval on an invoice")
    parser.add_argument("--invoice_path", required=True, type=Path)
    args = parser.parse_args()

    invoice = extract_invoice(args.invoice_path)
    validation = validate_invoice(invoice)
    result = approve_invoice(invoice, validation)
    print(json.dumps(result.model_dump(), indent=2))
