from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, PlainSerializer, model_validator

from app.extraction.schema import ValidationIssue


class ApiError(BaseModel):
    code: str
    message: str
    retryable: bool | None = None


class ModelVersions(BaseModel):
    ocr: str | None = None
    extractor: str | None = None


Num = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json-unless-none")]


class ResultItem(BaseModel):
    description: str | None = None
    quantity: Num | None = None
    unit: str | None = None
    unit_price: Num | None = None
    discount: Num | None = None
    tax: Num | None = None
    amount: Num | None = None


class ResultPayload(BaseModel):
    """API rendering of InvoiceData (schema v2): money as JSON numbers (storage keeps exact decimal strings).
    Fields that were not found are omitted, so v1 clients see the same keys as before plus optional new ones."""
    document_type: str | None = None
    document_number: str | None = None
    invoice_number: str | None = None
    date: dt.date | None = None
    date_bs: str | None = None
    due_date: dt.date | None = None
    currency: str | None = None
    language: str | None = None
    vendor: str | None = None
    supplier_address: str | None = None
    supplier_phone: str | None = None
    supplier_email: str | None = None
    supplier_tax_id: str | None = None
    customer_name: str | None = None
    customer_address: str | None = None
    customer_id: str | None = None
    customer_tax_id: str | None = None
    subtotal: Num | None = None
    discount: Num | None = None
    taxable_amount: Num | None = None
    tax: Num | None = None
    service_charge: Num | None = None
    other_charges: Num | None = None
    total: Num | None = None
    amount_paid: Num | None = None
    amount_due: Num | None = None
    items: list[ResultItem] = Field(default_factory=list)


class PipelineVersions(BaseModel):
    preprocessing: str
    schema_version: str
    normalization: str | None = None
    validation: str | None = None
    pipeline: str | None = None
    layout: str | None = None
    confidence_method: str | None = None


class ProcessDetail(BaseModel):
    process_id: str
    status: str
    batch_id: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None = None
    attempts: int = 0
    filename: str | None = None
    reviewed: bool | None = Field(default=None, description="True once a person reviewed the document (see feedback `reviewed`)")
    reviewed_at: dt.datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    result: ResultPayload | None = None
    confidence: float | None = Field(default=None, description="PROVISIONAL heuristic score in [0,1]. Not a calibrated probability.")
    confidence_calibrated: bool | None = None
    field_confidence: dict[str, float] | None = None
    requires_review: bool | None = None
    issues: list[ValidationIssue] | None = None
    model_versions: ModelVersions | None = None
    pipeline_versions: PipelineVersions | None = None
    error: ApiError | None = None


class BatchDetail(BaseModel):
    batch_id: str
    status: str
    total: int
    rejected_at_intake: int
    counts: dict[str, int]
    created_at: dt.datetime
    processes: list[dict[str, Any]]


class Correction(BaseModel):
    field: str = Field(max_length=64)
    value: Any = None  # None = "this field should be empty"


class FeedbackRequest(BaseModel):
    corrections: list[Correction] = Field(default_factory=list, max_length=100)
    comment: str | None = Field(default=None, max_length=1000)
    reviewed: bool = Field(default=False, description="True when a person checked the WHOLE document: fields that are not corrected "
                           "are confirmed as extracted. With `reviewed=true` the corrections list may be empty (confirmed as extracted). "
                           "A document can be reviewed once.")

    @model_validator(mode="after")
    def _something_to_submit(self):
        if not self.corrections and not self.reviewed:
            raise ValueError("Send at least one correction, or set reviewed=true to confirm the extraction")
        return self


class FeedbackResponse(BaseModel):
    submission_id: str
    accepted: int
    feedback_ids: list[str]
    reviewed: bool = False


class ListItem(BaseModel):
    """One row of the document list. Headline values come from the stored extraction (null until the document is completed)."""
    process_id: str
    status: str
    filename: str | None = None
    created_at: dt.datetime
    reviewed: bool = False
    reviewed_at: dt.datetime | None = None
    vendor: str | None = None
    invoice_number: str | None = None
    date: dt.date | None = None
    total: Num | None = None
    currency: str | None = None


class ListResponse(BaseModel):
    items: list[ListItem]
    next_cursor: str | None = Field(default=None, description="Pass as `cursor` to get the next (older) page")


class SubmitAccepted(BaseModel):
    process_id: str = Field(examples=["k3f9a7e5b1d4c2e8a6b7"])
    status: str = Field(examples=["queued"])
    created_at: dt.datetime


class BulkItem(BaseModel):
    index: int
    filename: str | None = None
    process_id: str | None = Field(default=None, description="null when the file was rejected")
    status: str = Field(description="queued | rejected", examples=["queued"])
    error: dict[str, Any] | None = Field(default=None, examples=[{"code": "unsupported_file_type", "message": "Only PNG, JPEG and PDF files are accepted"}])


class BulkAccepted(BaseModel):
    batch_id: str = Field(examples=["batch_0f3c9a7e5b1d4c2e8a6b7d9e0f1a2b3c"])
    status: str = Field(examples=["queued"])
    accepted: int
    rejected: int
    created_at: dt.datetime
    processes: list[BulkItem]
