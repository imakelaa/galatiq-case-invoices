# galatiq-case-invoices

Please use this link to schedule time: https://calendar.app.google/yKDVMn7sXfjD6UED7


Thanks,
Barric


—


Timeline and Expectations:
Build a working prototype, not just designs or slides. It must run end-to-end in code.
Tomorrow, you'll present to us (either myself or myself + other Galatiq members) 30 minutes: Demo your system running on sample inputs, explain your architecture/decisions, and handle questions. (We'll schedule this via email.)
Focus on shipping something valuable quickly under ambiguity-ruthlessly cut scope if needed, but ensure it's production-oriented (clean code, error handling, observability).
Use xAI's Grok as the core reasoning engine (via the xAI API at https://grok.x.ai - other models are also acceptable if you don't have an API key). Incorporate multi-agent orchestration (e.g., LangGraph, CrewAI, AutoGen, or custom), function calling/tool use, structured outputs, and self-correction loops.
Assume no internet for external APIs during runtime (simulate everything locally).
Tech stack: Python (preferred), with libraries like langchain, crewai, autogen, requests (for mock APIs), etc. Deploy nothing to cloud-run locally.

Problem Scenario: You're deployed to Acme Corp, a PE-backed manufacturing firm losing $2M/year on manual invoice processing. Invoices arrive via email as PDFs (messy formats, errors common). Staff manually extract data, validate against a legacy inventory database (inconsistent), get VP approval (via email chains), and process payment (via a mock banking API). Issues: 30% error rate, 5-day delays, angry stakeholders.

Current "hellscape" (mapped for you-normally you'd do this onsite):

Invoices: PDFs with fields like Vendor, Amount, Items (list), Due Date. Errors: Typos, missing data, fraud risks.
Validation: Check items against inventory DB (SQLite mock provided below). Flag mismatches (e.g., quantity > stock).
Approval: Simulate VP review-agent decides based on rules (e.g., >$10K needs human-like critique).
Payment: Call a mock API to "pay" if approved.
Exceptions: Handle bad data, retries, logging.

Task: Build a Multi-Agent System to Automate This. Reimagine the workflow as an agentic system:

Ingestion Agent: Extract data from PDF invoice (use a library like PyMuPDF or pdfplumber for parsing-handle unstructured text).
Validation Agent: Use Grok to reason over extracted data, call tools to query mock DB, flag issues with self-correction (e.g., if mismatch, retry parse or query).
Approval Agent: Orchestrate reflection/critique loop with Grok (e.g., generate pros/cons, decide approve/reject).
Payment Agent: If approved, call mock payment function; else, log rejection.
Orchestrate as multi-agent (e.g., via CrewAI tasks or LangGraph graph).
Include observability: Log agent steps, errors, decisions to a file (e.g., JSON).
Self-correction: If an agent fails (e.g., parse error), loop back with Grok for fixes.
Handle ambiguity: System should work on "messy" inputs (provide 3 sample PDFs below-create them yourself or use text mocks if PDF parsing is tricky in 24h).

Provided Resources (All Self-Contained):

Mock Invoice Data: Use these as inputs (save as .pdf or .txt for testing).
Invoice1: "Vendor: Widgets Inc. Amount: 5000. Items: WidgetA:10, WidgetB:5. Due: 2026-02-01" (clean).
Invoice2: "Vndr: Gadgets Co. Amt: 15000. Itms: GadgetX:20 (typo in stock). Due: 2026-01-30" (messy, >$10K).
Invoice3: "Vendor: Fraudster. Amount: 100000. Items: FakeItem:100. Due: yesterday" (invalid, should reject).
Mock Inventory DB: Create a local SQLite DB with this schema/data (code snippet below to initialize):
Python


import sqlite3
conn = sqlite3.connect(':memory:')  # Or save to file
conn.execute('''CREATE TABLE inventory (item TEXT, stock INTEGER);''')
conn.execute("INSERT INTO inventory VALUES ('WidgetA', 15), ('WidgetB', 10), ('GadgetX', 5), ('FakeItem', 0);")
conn.commit()
# Tool function example: def query_db(item): return conn.execute(f"SELECT stock FROM inventory WHERE item='{item}'").fetchone()[0]




Mock Payment API: Simple function:
Python


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}  # Or raise error randomly for testing




Grok API Setup: Use xAI's SDK (pip install xai-sdk, other another model sdk as needed). Example call:
Python
from xai import Grok
client = Grok(api_key="your_key")
response = client.chat.completions.create(model="", messages=[{"role": "user", "content": "Reason about this..."}])

Deliverables (Submission Format): Submit everything as a single ZIP file to recruiting@galatiq.ai & barric@galatiq.ai with the subject "Trent's Galatiq Case Submission". Include:

code/ folder: All Python files for the system (e.g., main.py to run end-to-end, agents.py, tools.py). Must run with python main.py --invoice_path=invoice1.txt and output logs/results.
demo_script.md: Markdown file with:
Architecture diagram (ASCII or simple text).
Step-by-step explanation of your design (e.g., why this orchestration? How does self-correction work? Tradeoffs cut for time?).
Run logs for all 3 sample invoices (copy-paste output).
Edge cases handled (e.g., bad data rejection).
presentation.md: Bullet-point script for your live demo (5-10 min): What you'll show, key code snippets, business impact (e.g., "Reduces errors by X%").

What matters: Functionality (does it work?), Code quality (clean, testable), Agentic sophistication (Grok integration, multi-agent flow, tools/loops), Shipping mindset (valuable MVP in 24h), and Presentation (clear translation of tech to business).




Questions? Reply briefly - but remember, real work has ambiguity!




