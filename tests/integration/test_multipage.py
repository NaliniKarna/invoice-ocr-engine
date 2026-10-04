"""Multi-page documents through the real API -> worker path: ordering, page metadata, partial failure, traceability."""
from app.models import OcrResult
from app.ocr.base import OCRLine, OCRPage, OCRPermanentError, OCRProvider, OCRTransientError
from app.workers.job import Outcome, WorkerContext, run_job
from tests.fakes import INVOICE_LINES, pdf_bytes
from tests.helpers import upload
from app.extraction.rules import RuleBasedExtractor


class PerPageOCR(OCRProvider):
    name, version = "perpage-ocr", "1"

    def __init__(self, fail_pages=(), transient_pages=()):
        self.fail_pages, self.transient_pages, self.seen = set(fail_pages), set(transient_pages), []

    def recognize_page(self, image, number):
        self.seen.append(number)
        if number in self.transient_pages:
            raise OCRTransientError("busy")
        if number in self.fail_pages:
            raise OCRPermanentError("page exploded at /srv/x")
        lines = INVOICE_LINES if number == 1 else [f"Page {number} continuation"]
        return OCRPage(number, image.width, image.height, [OCRLine(t, 0.9, (0, i * 20, 100, i * 20 + 18)) for i, t in enumerate(lines)])


def ctx_for(container, ocr):
    return WorkerContext(container.settings, container.session_factory, container.storage, ocr, RuleBasedExtractor())


def test_three_page_pdf_keeps_order_and_page_metadata(client, container, tenant_a, queue):
    pid = upload(client, tenant_a, pdf_bytes(3), "b.pdf", "application/pdf").json()["process_id"]
    ocr = PerPageOCR()
    assert run_job(queue.items.pop(), ctx_for(container, ocr)).outcome is Outcome.COMPLETED
    assert ocr.seen == [1, 2, 3]
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        assert res.page_count == 3 and [p["page"] for p in res.meta["pages"]] == [1, 2, 3] and {p["status"] for p in res.meta["pages"]} == {"ok"}
        assert res.meta["evidence"]["total"]["page"] == 1                        # field traceable to its page


def test_one_failed_page_does_not_corrupt_the_document_and_forces_review(client, container, tenant_a, queue):
    pid = upload(client, tenant_a, pdf_bytes(3), "b.pdf", "application/pdf").json()["process_id"]
    assert run_job(queue.items.pop(), ctx_for(container, PerPageOCR(fail_pages={2}))).outcome is Outcome.COMPLETED
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["status"] == "completed" and body["requires_review"] is True
    assert any(i["code"] == "page_ocr_failed" and i["severity"] == "error" for i in body["issues"])
    assert "/srv" not in client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        assert [p["status"] for p in res.meta["pages"]] == ["ok", "failed", "ok"] and res.meta["failed_pages"] == [2]
        assert res.meta["pages"][1]["error_code"] == "ocr_failed"
        assert res.data["total"] is not None                                     # readable pages still produced data


def test_all_pages_failed_fails_the_job_with_safe_message(client, container, tenant_a, queue):
    pid = upload(client, tenant_a, pdf_bytes(2), "b.pdf", "application/pdf").json()["process_id"]
    assert run_job(queue.items.pop(), ctx_for(container, PerPageOCR(fail_pages={1, 2}))).outcome is Outcome.FAILED
    text = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text
    assert "/srv" not in text and client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["status"] == "failed"


def test_transient_error_on_any_page_retries_the_whole_job(client, container, tenant_a, queue):
    upload(client, tenant_a, pdf_bytes(2), "b.pdf", "application/pdf")
    assert run_job(queue.items.pop(), ctx_for(container, PerPageOCR(transient_pages={2}))).outcome is Outcome.RETRY


def test_partial_page_policy_fail_makes_partial_documents_fail(client, container, tenant_a, queue):
    pid = upload(client, tenant_a, pdf_bytes(2), "b.pdf", "application/pdf").json()["process_id"]
    s = container.settings.model_copy(update={"partial_page_policy": "fail"})
    ctx = WorkerContext(s, container.session_factory, container.storage, PerPageOCR(fail_pages={2}), RuleBasedExtractor())
    assert run_job(queue.items.pop(), ctx).outcome is Outcome.FAILED


def test_enabled_preprocessing_ops_are_recorded_in_the_result_version(client, container, tenant_a, queue):
    """Regression: the version string grows with the enabled ops and must fit the column (PostgreSQL enforces length)."""
    s = container.settings.model_copy(update={"preprocess_ops": "autocontrast,median3,sharpen,otsu,deskew"})
    pid = upload(client, tenant_a, pdf_bytes(1), "b.pdf", "application/pdf").json()["process_id"]
    ctx = WorkerContext(s, container.session_factory, container.storage, PerPageOCR(), RuleBasedExtractor())
    assert run_job(queue.items.pop(), ctx).outcome is Outcome.COMPLETED
    v = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["pipeline_versions"]["preprocessing"]
    assert v.startswith("1+ops1:") and "autocontrast" in v and "deskew" in v
