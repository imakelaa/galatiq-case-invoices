# Invoice Processing Automation

A four-stage multi-agent pipeline (ingestion → validation → approval → payment) that
processes messy/fraudulent invoices end-to-end, built for the Galatiq case study
(see `problem_statement/README.md` for the original brief).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in XAI_API_KEY (https://console.x.ai)

python3 db/setup_inventory.py   # creates db/inventory.db (inventory + payments ledger)
```

## Usage

```bash
# single invoice
python3 main.py --invoice_path=data/invoices/invoice_1001.txt

# every supported file in a directory (.txt, .json, .csv, .xml, .pdf)
python3 main.py --invoice_dir=data/invoices

# stream node-by-node agent reasoning (LangGraph steps, tool calls) to stderr
python3 main.py --invoice_path=data/invoices/invoice_1001.txt --verbose
```

In directory mode, a failure on one invoice is logged to stderr and the rest of the
batch keeps running. Each agent can also be run standalone for debugging — see the
`if __name__ == "__main__"` block at the bottom of each `agents/*.py` file.

## The agents

- **Ingestion** (`agents/ingestion.py`) — Reads a raw invoice file (plain text, or PDF
  via `pdfplumber`) and calls Grok with a structured-output schema to extract
  `InvoiceData`. Retries once if the response fails schema validation.
- **Validation** (`agents/validation.py`) — A LangGraph ReAct agent that looks up each
  line item against `db/inventory.db` via a `query_inventory` tool. Date math (due-date
  calculation, mismatch/fraud flag construction) is done deterministically in Python,
  not by the LLM.
- **Approval** (`agents/approval.py`) — A propose → critique LangGraph loop (max 1
  revision) simulates VP review, using vendor payment history from the ledger. Invoices
  at/above the $10K threshold are force-routed to `needs_review` regardless of what the
  model proposes, and an invoice number that's already been paid is short-circuited to
  `needs_review` rather than silently re-approved.
- **Payment** (`agents/payment.py`) — On `approve`, calls the mock payment API and
  decrements `db/inventory.db` stock for the shipped items. On `reject`/`needs_review`,
  logs the reasoning instead. Every outcome is recorded in the `payments` ledger table.

## Observing results

- **Console output** — `main.py` prints each stage's structured JSON result
  (`InvoiceData` → `ValidationResult` → `ApprovalResult` → `PaymentResult`) as it runs.
- **`logs/review.log`** — every invoice that did *not* get paid (`rejected` or
  `needs_review`), with the reasoning and a timestamp — the queue a human would work.
- **`logs/pipeline.log`** — the full audit trail: one JSON entry per invoice with what
  every stage saw and decided, including paid invoices.
- **`db/inventory.db`** — the source of truth: `inventory` (current stock),
  `payments` (the ledger every agent reads/writes), and `vendors` (a view over
  `payments` used for vendor-history and structuring checks).

## Tests

```bash
pytest
```

Covers the deterministic logic in each agent (date/flag math, ledger upserts and
normalization, log formatting, payment routing) with a temp SQLite DB per test — no
LLM calls, so it runs without an API key. See `tests/`.
