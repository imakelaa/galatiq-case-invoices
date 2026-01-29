# Galatiq Case: Invoice Processing Automation

## Background

Acme Corp is a PE-backed manufacturing firm losing **$2M/year** on manual invoice processing. Invoices arrive via email as PDFs in messy formats with frequent errors. Staff manually extract data, validate against a legacy inventory database (inconsistent), obtain VP approval (via email chains), and process payment (via a banking API).

**Current pain points:**
- 30% error rate
- 5-day processing delays
- Frustrated stakeholders

## Objective

Build a **multi-agent system** that automates the end-to-end invoice processing workflow. The system must run as a working prototype — not just designs or slides.

## Workflow

The system should handle four stages:

1. **Ingestion** — Extract structured data from invoice documents (PDFs, text files). Fields include: Vendor, Amount, Items (with quantities), and Due Date. Expect unstructured text, typos, missing data, and potentially fraudulent entries.

2. **Validation** — Verify extracted data against a mock inventory database (SQLite). Flag mismatches such as quantity exceeding available stock or items not found in inventory.

3. **Approval** — Simulate VP-level review with rule-based decision-making (e.g., invoices over $10K require additional scrutiny). The agent should reason through approval/rejection with a reflection or critique loop.

4. **Payment** — If approved, call a mock payment function. If rejected, log the rejection with reasoning.

## Technical Requirements

- **LLM Integration**: Use xAI's Grok as the core reasoning engine (via the xAI API at https://grok.x.ai). Other models are acceptable if you don't have an API key.
- **Multi-Agent Orchestration**: Use a framework such as LangGraph, CrewAI, AutoGen, or a custom solution.
- **Agent Capabilities**: Function calling / tool use, structured outputs, and self-correction loops.
- **Runtime**: Assume no internet for external APIs — simulate everything locally.
- **Tech Stack**: Python (preferred), with libraries like `langchain`, `crewai`, `autogen`, `pdfplumber`, `PyMuPDF`, etc. Run locally — no cloud deployment.

## Provided Resources

### Mock Invoice Data

Sample invoices are provided in the `data/invoices/` directory in various formats (PDF, CSV, JSON, TXT). Use these as inputs for testing. The data intentionally includes a mix of clean entries and problematic ones — identifying and handling issues is part of the challenge.

### Mock Inventory Database

Create a local SQLite database with the following schema:

```python
import sqlite3

conn = sqlite3.connect(':memory:')  # Or save to file
conn.execute('CREATE TABLE inventory (item TEXT, stock INTEGER)')
conn.execute("""
    INSERT INTO inventory VALUES
    ('WidgetA', 15),
    ('WidgetB', 10),
    ('GadgetX', 5),
    ('FakeItem', 0)
""")
conn.commit()
```

### Mock Payment API

```python
def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
```

### Grok API Setup

```python
from xai import Grok

client = Grok(api_key="your_key")
response = client.chat.completions.create(
    model="grok-3",
    messages=[{"role": "user", "content": "Reason about this..."}]
)
```

## Running the System

The system should be executable from the command line:

```bash
python main.py --invoice_path=data/invoices/invoice1.txt
```

Output should include structured logs and results.

## Deliverables

| File / Folder | Description |
|---|---|
| `code/` | All Python source files (`main.py`, `agents.py`, `tools.py`, etc.) |
| `demo_script.md` | Architecture diagram (ASCII/text), design explanation, run logs for all sample invoices, edge cases handled |
| `presentation.md` | Bullet-point script for a live demo (5-10 min): what you'll show, key code snippets, business impact |

## Evaluation Criteria

- **Functionality** — Does the system work end-to-end?
- **Code Quality** — Clean, testable, well-structured code with error handling and observability
- **Agentic Sophistication** — LLM integration, multi-agent flow, tool use, self-correction loops
- **Shipping Mindset** — Valuable MVP delivered under ambiguity; scope ruthlessly cut where needed
- **Presentation** — Clear translation of technical decisions to business impact
