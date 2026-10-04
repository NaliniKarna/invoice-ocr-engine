"""Sandbox child: decode/render untrusted document (stdin) to length-prefixed PNG pages (stdout)."""
import app.security.limits_boot  # noqa: F401,E402  (must be first)
import io
import json
import struct
import sys

from app.ocr.preprocess import load_pages


def main() -> None:
    cfg = json.loads(sys.argv[1])
    try:
        pages = load_pages(sys.stdin.buffer.read(), cfg["ct"], max_pages=cfg["max_pages"], max_pixels=cfg["max_pixels"],
                           ops=cfg.get("ops") or [])
    except Exception:  # any failure -> single marker byte; parent raises a generic, safe error
        sys.stdout.buffer.write(b"E")
        return
    out = sys.stdout.buffer
    for img in pages:
        if img is None:                 # page failed to render: zero-length record keeps page numbering intact
            out.write(struct.pack(">I", 0)); continue
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        out.write(struct.pack(">I", buf.tell()) + buf.getvalue())


main()
