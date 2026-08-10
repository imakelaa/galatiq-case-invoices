"""Approval agent: simulates VP-level review with a propose/critique loop.

Approval agent relies on results of the validation agent.
If invoice has been validated, agent reasons through an approve/reject decision.

CURRENTLY: agent does 1 revision maximum before finalizing decision.
"""

import sys
from pathlib import Path
from typing import Literal, Optional, TypedDict

from langchain_xai import ChatXAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agents import ledger
from agents.ingestion import InvoiceData
from agents.validation import ValidationResult
from prompts import load_prompt

MODEL = "grok-4-fast"
MAX_REVISIONS = 1

PROPOSE_SYSTEM_PROMPT = load_prompt("approval_propose_system")
CRITIQUE_SYSTEM_PROMPT = load_prompt("approval_critique_system")


class Proposal(BaseModel):
    decision: Literal["approve", "reject"]
    reasoning: str = Field(description="Explanation for the decision, addressing the rules and any validation flags")


class CritiqueVerdict(BaseModel):
    verdict: Literal["confirm", "revise"]
    feedback: str = Field(description="Why confirmed, or what specifically needs to change if revising")


class ApprovalResult(BaseModel):
    invoice_number: Optional[str]
    decision: Literal["approve", "reject", "needs_review"]
    reasoning: str
    critique_rounds: int = Field(description="How many propose/critique cycles ran before finalizing")


class ApprovalState(TypedDict):
    invoice: InvoiceData
    validation: ValidationResult
    vendor_history: Optional[ledger.VendorStats]
    proposal: Optional[Proposal]
    critique: Optional[CritiqueVerdict]
    revision_count: int


def _vendor_history_block(vendor_history: Optional[ledger.VendorStats]) -> str:
    if not vendor_history:
        return "No prior payment history for this vendor."
    return (
        f"invoice_count={vendor_history['invoice_count']}, "
        f"paid_count={vendor_history['paid_count']}, "
        f"rejected_count={vendor_history['rejected_count']}, "
        f"avg_amount={vendor_history['avg_amount']:.2f}, "
        f"near_10k_threshold_count={vendor_history['near_threshold_count']}"
    )


def _context_block(state: ApprovalState) -> str:
    return (
        f"Invoice:\n{state['invoice'].model_dump_json(indent=2)}\n\n"
        f"Validation result:\n{state['validation'].model_dump_json(indent=2)}\n\n"
        f"Vendor payment history: {_vendor_history_block(state['vendor_history'])}"
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
    paid_record = ledger.get_paid_record(invoice.invoice_number)
    if paid_record is not None:
        return ApprovalResult(
            invoice_number=invoice.invoice_number,
            decision="needs_review",
            reasoning=(
                f"Invoice number {invoice.invoice_number} was already paid via "
                f"{paid_record['source_file']} (${paid_record['amount']:.2f}). This submission "
                f"({invoice.source_file}, ${invoice.amount if invoice.amount is not None else 'unknown'}) "
                "has the same invoice_number but differs in content, so it isn't necessarily a "
                "duplicate -- it could be a legitimate correction/resubmission. Not auto-deciding "
                "either way; flagging for manual review."
            ),
            critique_rounds=0,
        )

    app = build_graph()
    initial_state = {
        "invoice": invoice,
        "validation": validation,
        "vendor_history": ledger.get_vendor_stats(invoice.vendor),
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
