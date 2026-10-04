"""Strategies for benchmarks/layout_bench.py that exist only after the layout-aware work."""
from app.extraction.pipeline import DocumentPipeline, RetryPlan
from app.extraction.validation import ValidationConfig
from app.ocr.base import OCRProvider
from benchmarks.layout_bench import PIPE_PRIMARY, PIPELINES, _single, pipeline, strategy


@strategy("layout-kv")
def _s_kv(ocr, images):
    from app.extraction.layout_kv import LayoutKVExtractor
    return _single(LayoutKVExtractor)(ocr, images)


class _Fixed(OCRProvider):
    """Perfect OCR (oracle mode): returns the stored result whatever the image."""
    name, version = "oracle", "1"

    def __init__(self, result):
        self.result = result

    def recognize(self, pages):
        return self.result


def _make(primary: str, alternates: tuple[str, ...], retry: bool):
    from app.core.container import extractor_by_name
    from app.extraction.rules import RuleBasedExtractor
    from app.ocr.providers import TesseractProvider

    def run(images, oracle):
        ocr = _Fixed(oracle) if oracle is not None else TesseractProvider()
        plans = None
        if retry and oracle is None:
            plans = [RetryPlan(TesseractProvider(psm=3), ["upscale"], psm=3), RetryPlan(TesseractProvider(psm=6), ["upscale", "adaptive"], psm=6)]
        p = DocumentPipeline(ocr, extractor_by_name(primary), day_first=True, review_threshold=0.8, fallback=RuleBasedExtractor(),
                             validation=ValidationConfig(required_fields=True), retry=plans, alternates=[extractor_by_name(a) for a in alternates])
        return p.run(list(images))
    return run


for _name, _primary, _alts, _retry in (
        ("pipe:rules+gate", "layout-rules", ("layout-kv",), False),        # old primary, new extractor as the alternate
        ("pipe:kv", "layout-kv", (), False),
        ("pipe:kv+alts", "layout-kv", ("layout-rules", "rule-based-baseline"), False),
        ("pipe:kv+alts+retry", "layout-kv", ("layout-rules", "rule-based-baseline"), True),
        ("pipe:rules+retry", "layout-rules", (), True)):                   # current retry design (confidence trigger) is not part of this; same retry plan
    PIPELINES[_name] = _make(_primary, _alts, _retry)
    PIPE_PRIMARY[_name] = _primary
