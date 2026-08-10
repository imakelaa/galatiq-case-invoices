# Technical Specification — Invoice Processing Automation

This doc is just *what's actually there*: the pipeline, the data
stores, the observability layer, and what I deliberately didn't build.

Status: working prototype, CLI-driven, single machine, local SQLite. Built
for the Galatiq case study (`problem_statement/README.md`).

---

## 1. System overview

Four stages, run one invoice at a time, wired together by `main.py`:

```
                 ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
  raw file  ───▶ │  INGESTION  │ ──▶ │ VALIDATION  │ ──▶ │  APPROVAL   │ ──▶ │  PAYMENT    │
 (.txt/.json/    │ agents/     │     │ agents/     │     │ agents/     │     │ agents/     │
  .csv/.xml/     │ ingestion.py│     │ validation.py│    │ approval.py │     │ payment.py  │
  .pdf)          └─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                       │                    │                    │                    │
                  InvoiceData         ValidationResult      ApprovalResult       PaymentResult
                  (Pydantic)          (Pydantic)            (Pydantic)           (Pydantic)
```

Each stage just takes the previous stage's typed output and produces its
own. They don't share state directly — the only thing connecting them
outside of that handoff is the SQLite ledger (`db/inventory.db`): validation
reads stock from it, approval reads vendor history and checks for
duplicates, payment writes stock decrements and the payment record.

You run it via `main.py`, either one invoice at a time (`--invoice_path`) or
as a batch (`--invoice_dir`, which runs invoices concurrently with a
`ThreadPoolExecutor(max_workers=3)`). There's no server process sitting
around — every run is a fresh CLI invocation that starts and exits.

---

## 2. Stage 1 — Ingestion (`agents/ingestion.py`)

Takes a file path, returns `InvoiceData`.

For a `.pdf` I pull the text out with `pdfplumber` (page by page, then
joined). Everything else — `.txt/.json/.csv/.xml` — I just read as plain
text and hand to the model as-is. I didn't bother writing a fast path for
already-structured formats like JSON/CSV; it goes through the same LLM call
as everything else.

That call is a single `xai_sdk` `chat.parse(InvoiceData)` against
`grok-4-fast`, with a system prompt (`prompts/ingestion_system.md`) telling
it to extract exactly what's on the page and not invent or "fix" anything —
if a field's missing or unreadable, it comes back null.

If the response fails Pydantic validation, I send the error back in the
same chat turn and give it one retry (`MAX_EXTRACTION_ATTEMPTS = 2` total —
so two attempts, not two retries). That retry gets logged via
`telemetry.log_retry`. If it still fails after that, it raises and the
invoice fails outright — no partial result.

The schema itself (`InvoiceData`) is `invoice_number`, `vendor`, `amount`,
`invoice_date`, `due_date`, `payment_terms` (all optional, since messy
invoices routinely drop one of these), plus `items: list[LineItem]` (just
`item` and `quantity`) and `source_file`. This stage doesn't judge or
validate anything — it's extraction only.

---

## 3. Stage 2 — Validation (`agents/validation.py`)

Takes `InvoiceData`, returns `ValidationResult`.

I split this one on purpose: the model classifies, and Python does the
actual math (see `tech_dec.md` for why — I didn't trust the model to get
date arithmetic right consistently, and it doesn't need to).

The LLM half is a LangGraph ReAct agent (`grok-4-fast` via `langchain_xai`)
with one tool, `query_inventory(item)`, which looks up
`db/inventory.db.inventory` with a normalized match (case/punctuation/
whitespace stripped — no fuzzy guessing beyond that). It ends its turn with
a structured output (`AgentOutput`): per-item inventory flags, plus how it's
reading the dates — `invoice_date_issue`/`due_date_issue` (`missing` or
`fraudulent`), `payment_terms_type` (`net_days`/`end_of_month`/`immediate`/
`unrecognized`), and `payment_terms_days`. If it ends its turn without
calling that structured output, I give it one retry
(`MAX_VALIDATION_ATTEMPTS = 2`); otherwise it raises.

Everything after that — the actual date math and flag construction — is
plain Python in `_build_validation_result`, not the model:

| Flag type | category | blocking | when it fires |
|---|---|---|---|
| `unknown_item` | — | yes | item isn't in the inventory DB |
| `insufficient_stock` | — | yes | requested qty > stock on hand |
| `invalid_quantity` | — | yes | qty <= 0 |
| `missing_invoice_date` | `invalid` | yes | invoice_date is absent — can't pay this |
| `unparseable_date` | `fraudulent` | yes | invoice_date or due_date is vague/relative/corrupted ("yesterday", "ASAP") |
| `due_date_mismatch` | `discrepant` | no | stated due_date disagrees with invoice_date + terms |
| `missing_due_date` | `missing` | no | due_date and terms both absent; I default `effective_due_date` to invoice_date + 30 days |

`passed` is just `not any(flag.blocking for flag in flags)` — the two
non-blocking flags are informational, they don't fail validation on their
own.

For the date math, I used `dateutil.relativedelta` for N-day terms that are
multiples of 30, and a literal day-count otherwise. I tried a fixed offset
first and it broke immediately — Net 30 needs a +1 in a 31-day month, Net 60
spanning Jan→Feb needs a −1 — so calendar-month semantics it is. One other
small thing: if a date has a single OCR-swapped character but is otherwise
clearly a real date, I correct it silently rather than flagging it as
fraudulent — that felt like a data-quality issue, not a fraud signal.

---

## 4. Stage 3 — Approval (`agents/approval.py`)

Takes `InvoiceData` + `ValidationResult`, returns `ApprovalResult`
(`decision: approve | reject | needs_review`).

Before any LLM call happens, I check `ledger.get_paid_record(invoice_number)`
— if this invoice number has already been paid once, I don't try to guess
whether this new submission is a duplicate or a legitimate resubmission
(e.g. `invoice_1004.json` vs `invoice_1004_revised.json` — same number,
different content). I just send it straight to `needs_review` and let a
human sort it out. No LLM call, no auto-anything.

If it's not a repeat, it goes through a propose → critique loop in
LangGraph, meant to simulate a second reviewer catching a bad call:

1. `propose_node` — `grok-4-fast` decides approve/reject with reasoning,
   given the invoice, the validation result, and the vendor's payment
   history (`ledger.get_vendor_stats`: invoice count, paid/rejected counts,
   average amount, how many invoices have landed near the $10K threshold).
2. `critique_node` — a second model call reviews that proposal and comes
   back with `confirm` or `revise` plus feedback.
3. If it says `revise` and we haven't hit `MAX_REVISIONS` (1), it loops back
   to `propose_node` with that feedback attached; otherwise it's final.

One thing I don't leave up to the model at all: if the proposed decision is
`approve` and the invoice amount is $10,000 or more, I force it to
`needs_review` in code, regardless of what the model's reasoning said. This
is the actual mechanism behind "VP approval requires additional scrutiny" —
see §9, it's a stop-and-flag, not a real approval workflow.

The prompt (`prompts/approval_propose_system.md`) tells the model to trust
each flag's `blocking` value instead of re-deriving severity itself, to
always reject when `invoice_date` is missing, to lean toward rejecting on
fraudulent dates or a vendor pattern of invoices clustered just under $10K
("structuring" — 2 or more in the $8K–$10K band), and to use
`effective_due_date` rather than the raw `due_date` field.

---

## 5. Stage 4 — Payment (`agents/payment.py`)

Takes `InvoiceData` + `ValidationResult` + `ApprovalResult`, returns
`PaymentResult` (`status: paid | rejected | needs_review`).

- `approve` → calls `mock_payment(vendor, amount)` (just prints and returns
  `{"status": "success"}` — see §9, there's no real payment rail behind
  this) and decrements stock in `ledger.decrement_stock()` for each line
  item. I let stock go negative rather than clamping it at zero — if
  something got shipped despite an insufficient-stock flag, I want that
  visible, not hidden.
- `reject` → logged to `logs/review.log`, and the ledger gets a row with
  `status='rejected'`.
- `needs_review` → also logged to `logs/review.log`, but the ledger is
  **not** touched at all — nothing's actually been decided yet, and writing
  a row here would mess with duplicate detection later (the
  `payments.status` column only allows `paid`/`rejected` anyway).
- No matter which branch it takes, every outcome gets appended to
  `logs/pipeline.log` — that's the full record of what every stage saw and
  decided, for every invoice.

---

## 6. Data stores (`db/inventory.db`, SQLite)

| Table/view | Written by | Read by | Notes |
|---|---|---|---|
| `inventory` | `payment.py` (decrement) | `validation.py` (lookup) | seeded once by `db/setup_inventory.py`; stock can go negative |
| `payments` | `payment.py` (the only writer) | `ledger.py` helpers | upsert on `(invoice_number, source_file)`; also has `invoice_number_norm`/`vendor_norm` columns so matching isn't thrown off by formatting differences — auto-added/backfilled by `_ensure_normalized_columns()` if an older DB doesn't have them yet |
| `vendors` (view) | derived, not written directly | `approval.py`, `observability/data.py` | grouped by `vendor_norm`, using `MIN(vendor)` for display. Since it's a view over `payments` rather than its own table, there's nothing to keep in sync — vendor stats can't drift from the ledger |

I turned on WAL mode (`PRAGMA journal_mode=WAL`) and put a `threading.Lock`
around the two functions that actually write (`record_payment`,
`decrement_stock`), because once batch mode runs invoices concurrently,
multiple threads hitting SQLite at once will throw "database is locked"
without it.

---

## 7. Logging & observability

Three logs, each doing one job:

- **`logs/pipeline.log`** — the full audit trail. Every invoice, every
  stage's output, including the ones that got paid.
- **`logs/review.log`** — just the actionable queue: `rejected` and
  `needs_review` only, i.e. whatever a human actually needs to look at.
- **`logs/telemetry.log`** — engineering signal, not business signal:
  per-stage latency, whether the run succeeded or failed (and which stage
  broke, with the exception), and retry events. This one's only populated
  when you run through `main.py` — a standalone `agents/*.py` call won't
  write to it.

All three are written as back-to-back pretty-printed JSON objects rather
than JSON-lines or a JSON array, mostly because I wanted them readable if
you just open the file. `observability/data.py` parses that format by
walking the file with `json.JSONDecoder.raw_decode`.

There are two read-only Streamlit dashboards on top of these, both just
reading what the pipeline already wrote — neither one triggers a run or
touches anything mid-pipeline:

- `observability/user_observe_app.py` — the business-facing view: outcome
  KPIs, the review queue, which validation flags come up most, per-vendor
  stats including the structuring check.
- `observability/dev_observe_app.py` — the engineering-facing view:
  per-stage latency (avg/p95), run outcomes, latency over time, a failures
  table, a retries table, with a sidebar time-range filter since telemetry
  keeps every run forever instead of deduping like `pipeline_df` does.

Both have an auto-refresh option — off by default — that you can turn on
from the sidebar and pick a 2–30 second interval. It's built on
`st.fragment(run_every=...)`, which only reruns that one fragment of the
page rather than the whole script, so if you've got the pipeline running in
another terminal, results show up without you touching the browser.

---

## 8. Concurrency model

Batch mode runs invoices through a `ThreadPoolExecutor(max_workers=3)`. I
capped it at 3 on purpose — not for throughput, but because higher
concurrency was tripping xAI rate limits, and those didn't show up as a
clean 429; they showed up as the model silently skipping its structured
output call, which is a worse failure mode to debug.

Each `run_pipeline()` call builds its output as a string and returns it
instead of printing directly, since multiple threads printing at once would
interleave and garble the output.

If one invoice in a batch fails, it's caught, logged to stderr, and the
rest of the batch keeps going.

---

## 9. Out of scope (not implemented)

Things I didn't build, either as a deliberate scope cut or because the case
study didn't call for them:

**No user-facing app or upload flow.** There's no web app, no API, no UI for
uploading a file or pointing at a directory. It's the CLI
(`python3 main.py --invoice_path=... / --invoice_dir=...`), plus the two
read-only dashboards that only visualize logs after the fact.

**"VP approval" doesn't actually route to a VP.** The approval agent
simulates the reasoning a reviewer would go through, but there's no real
human-in-the-loop mechanism behind it. Hitting the $10,000 threshold (or a
resubmitted invoice number) doesn't notify anyone or create a ticket — it
sets `decision = needs_review`, writes a line to `logs/review.log`, and
stops there. A person is expected to go read that log and act on it outside
the system; there's no approve/reject UI, no notification, and no way for
that person's decision to feed back into the pipeline. A resubmission has to
come in as a brand-new file.

**No cost/token optimization.** Every file — even a clean JSON one — goes
through a full LLM extraction call; I didn't write a fast path for
already-structured formats. Approval alone costs at least 2 LLM calls
(propose + critique), up to 4 if it revises once. No prompt caching, no
cheaper-model fallback, no token or cost tracking anywhere.

**No production-grade retry/reliability handling.** One retry per stage,
and it's just "send the error back, ask again" — no backoff, no jitter, no
circuit breaker. `BATCH_WORKERS = 3` was picked by trial and error, not from
an actual rate-limit budget. If a stage exhausts its retry, the whole
invoice fails with nothing salvaged.

**No real payment rail.** `mock_payment()` always prints and returns
success — there's no actual banking integration, no handling for a payment
API returning an error, and no idempotency beyond whatever the ledger's
upsert already gives you.

**No pricing or vendor-legitimacy validation.** Validation only checks
quantity against stock and whether the item exists — no unit-price checks,
no PO matching, no vendor allowlist. "Vendor legitimacy" is just the LLM
reasoning qualitatively from payment history, nothing more rigorous.

**No OCR for scanned PDFs.** PDF extraction is `pdfplumber` text extraction
only. A scanned, image-only PDF with no text layer will fail extraction or
come back as an all-null `InvoiceData`.

**No auth, multi-tenancy, or real secrets management.** One `XAI_API_KEY` in
a local `.env` file. The dashboards have no login and are meant to run
alongside the pipeline on your own machine, not be exposed anywhere.

**No horizontal scale-out.** One SQLite file, one machine, an in-process
thread pool. No job queue, no multi-worker or multi-host deployment, no
containerization.

**No eval framework.** The tests (`tests/`) only cover the deterministic
Python — date math, flag construction, ledger upserts, log formatting,
payment routing — against a temp SQLite DB, so they run without an API key.
There's no automated check of extraction/validation/approval *accuracy*
against the sample invoices' expected outcomes, and no regression suite for
when the prompts change.
