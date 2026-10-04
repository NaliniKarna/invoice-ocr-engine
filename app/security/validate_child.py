"""Sandbox child: validate untrusted bytes (stdin). Prints one JSON line. argv[1] = {"kind","limits":{...}}."""
import app.security.limits_boot  # noqa: F401,E402  (must be first)
import json
import sys

from app.security.content_checks import ContentRejected, Limits, check_image, check_pdf


def main() -> None:
    cfg = json.loads(sys.argv[1])
    lim = Limits(**cfg["limits"])
    data = sys.stdin.buffer.read()
    try:
        pages = check_pdf(data, lim) if cfg["kind"] == "pdf" else (check_image(data, lim) or 1)
        out = {"ok": True, "pages": pages}
    except ContentRejected as exc:
        out = {"ok": False, "code": exc.code, "message": exc.message}
    except MemoryError:
        out = {"ok": False, "code": "pdf_resource_limit", "message": "Document is too complex"}
    sys.stdout.write(json.dumps(out))


main()
