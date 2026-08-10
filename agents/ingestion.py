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

from prompts import load_prompt

load_dotenv()

MODEL = os.getenv("XAI_MODEL", "grok-4-fast")

SYSTEM_PROMPT = load_prompt("ingestion_system")


class LineItem(BaseModel):
    item: str = Field(description="Item name exactly as it appears on the invoice")
    quantity: int = Field(description="Quantity ordered, as written (may be negative or invalid)")


class InvoiceData(BaseModel):
    invoice_number: Optional[str] = Field(description="Invoice identifier, e.g. INV-1001")
    vendor: Optional[str] = Field(description="Vendor / sender name")
    amount: Optional[float] = Field(description="Total amount due, as stated on the invoice")
    invoice_date: Optional[str] = Field(description="Date the invoice was issued, exactly as written; null if missing")
    due_date: Optional[str] = Field(
        description="Due date exactly as written, only if explicitly stated; null if missing/unparseable"
    )
    payment_terms: Optional[str] = Field(
        description="Payment terms exactly as written, e.g. 'Net 30'; null if missing"
    )
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


SUPPORTED_EXTENSIONS = {".txt", ".json", ".csv", ".xml", ".pdf"}


def discover_invoice_files(dir_path: Path) -> list[Path]:
    return sorted(
        p for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Extract structured data from a single invoice file")
    parser.add_argument("--invoice_path", required=True, type=Path)
    args = parser.parse_args()

    invoice = extract_invoice(args.invoice_path)
    print(json.dumps(invoice.model_dump(), indent=2))
