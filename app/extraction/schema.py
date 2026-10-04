"""Canonical extraction result. Money is Decimal and is persisted as a decimal string in JSONB
(no float drift). Only the API response model (schemas/bills.py) renders JSON numbers."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Money = Decimal
SCHEMA_VERSION = "2"
# Results are stored with the schema version that produced them. v1 -> v2 is purely additive (new optional fields),
# so every v1 result validates as v2 and historical results stay interpretable without migration.
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1", "2"})


class LineItem(BaseModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Money | None = None
    discount: Money | None = None
    tax: Money | None = None
    amount: Money | None = None          # line total


class InvoiceData(BaseModel):
    """Canonical extraction result, schema v2. `vendor` is the supplier name (kept under its v1 name)."""
    # document
    document_type: Literal["invoice", "receipt", "credit_note", "other"] | None = None
    document_number: str | None = None
    invoice_number: str | None = None
    date: dt.date | None = None                      # Gregorian; converted from BS when the document uses Bikram Sambat
    date_bs: str | None = None                       # original BS text (YYYY-MM-DD) when the document date was BS
    due_date: dt.date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    # supplier
    vendor: str | None = None
    supplier_address: str | None = None
    supplier_phone: str | None = None
    supplier_email: str | None = None
    supplier_tax_id: str | None = None
    # customer
    customer_name: str | None = None
    customer_address: str | None = None
    customer_id: str | None = None
    customer_tax_id: str | None = None
    # money
    subtotal: Money | None = None
    discount: Money | None = None
    taxable_amount: Money | None = None
    tax: Money | None = None
    service_charge: Money | None = None
    other_charges: Money | None = None
    total: Money | None = None                       # grand total
    amount_paid: Money | None = None
    amount_due: Money | None = None
    items: list[LineItem] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    code: str
    field: str | None = None
    severity: Literal["warning", "error"] = "warning"
    message: str


class FieldEvidence(BaseModel):
    """Where a value came from and what was done to it. Internal: never returned by the public API."""
    raw: str | None = None                       # text as extracted, before normalization
    value: str | None = None                     # normalized value rendered as text
    transforms: list[str] = Field(default_factory=list)
    page: int | None = None
    bbox: list[int] | None = None
    source_text: str | None = None
    method: str | None = None
    ocr_confidence: float | None = None          # raw engine confidence
    heuristic_confidence: float | None = None    # rule-based score (uncalibrated)
    calibrated_confidence: float | None = None   # only when an active calibrator exists for this pipeline


CONFIDENCE_METHOD = "heuristic-v2"


class ExtractionOutput(BaseModel):
    data: InvoiceData
    field_confidence: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    item_evidence: list[dict] = Field(default_factory=list)
    confidence_method: str = CONFIDENCE_METHOD
    calibrated: bool = False
    confidence: float = 0.0
    requires_review: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)
