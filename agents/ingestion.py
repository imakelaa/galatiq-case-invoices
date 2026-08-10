"""Ingestion agent: extracts structured invoice data from raw documents.

Ingests data from raw documents (across multiple formats- .txt, .json, .csv,
.xml, .pdf).

Does no processing or validation of data.
"""

import os
from pathlib import Path
from typing import Optional

import pdfplumber
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError
from xai_sdk import Client
from xai_sdk.chat import system, user

load_dotenv()

MODEL = os.getenv("XAI_MODEL", "grok-4-fast")

SYSTEM_PROMPT = """\
You are an invoice data extraction agent for an accounts payable system.
You will be given the raw text of a single invoice, which may be poorly
formatted, contain typos or OCR-style errors, be missing fields, or be
fraudulent. Extract exactly what is written — do not invent or correct
values. If a field is missing or unreadable, leave it null rather than
guessing.
"""


class LineItem(BaseModel):
    item: str = Field(description="Item name exactly as it appears on the invoice")
    quantity: int = Field(description="Quantity ordered, as written (may be negative or invalid)")


class InvoiceData(BaseModel):
    invoice_number: Optional[str] = Field(description="Invoice identifier, e.g. INV-1001")
    vendor: Optional[str] = Field(description="Vendor / sender name")
    amount: Optional[float] = Field(description="Total amount due, as stated on the invoice")
    due_date: Optional[str] = Field(description="Due date exactly as written; null if missing/unparseable")
    items: list[LineItem] = Field(default_factory=list, description="Line items with quantities")
    source_file: str = Field(description="Filename this data was extracted from")


def read_source_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    return path.read_text(errors="replace")


MAX_EXTRACTION_ATTEMPTS = 2  # initial attempt + 1 retry on validation failure


def extract_invoice(path: Path) -> InvoiceData:
    raw_text = read_source_text(path)

    client = Client(api_key=os.environ["XAI_API_KEY"])
    chat = client.chat.create(model=MODEL)
    chat.append(system(SYSTEM_PROMPT))
    chat.append(user(f"Raw invoice document ({path.name}):\n\n{raw_text}"))

    last_error: Exception | None = None
    for attempt in range(MAX_EXTRACTION_ATTEMPTS):
        try:
            _, parsed = chat.parse(InvoiceData)
            parsed.source_file = path.name
            return parsed
        except ValidationError as exc:
            last_error = exc
            if attempt + 1 < MAX_EXTRACTION_ATTEMPTS:
                chat.append(
                    user(
                        "Your last response failed schema validation with this "
                        f"error:\n\n{exc}\n\nFix the extraction and try again."
                    )
                )

    raise RuntimeError(
        f"Failed to extract valid invoice data from {path.name} after "
        f"{MAX_EXTRACTION_ATTEMPTS} attempts: {last_error}"
    ) from last_error


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Extract structured data from an invoice file")
    parser.add_argument("--invoice_path", required=True, type=Path)
    args = parser.parse_args()

    invoice = extract_invoice(args.invoice_path)
    print(json.dumps(invoice.model_dump(), indent=2))
