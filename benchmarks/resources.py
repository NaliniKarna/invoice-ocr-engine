"""Isolated latency / memory measurement per OCR engine.   python -m benchmarks.resources --n 12 --out benchmarks/results

Run it on an otherwise idle machine. Each engine runs in a FRESH process that only loads pre-rendered PNGs (the synthetic
generator is not in that process, so peak RSS reflects engine + model + images, not the benchmark harness).
Reports: cold start (import + model load + first page), warm latency p50/p95 per page, peak RSS, CPU count, model size on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def child(engine: str, folder: str) -> None:
    import resource
    t0 = time.perf_counter()
    from PIL import Image
    if engine == "tesseract":
        from app.ocr.providers import TesseractProvider
        prov = TesseractProvider()
    else:
        from app.ocr.providers import RapidOCRProvider
        prov = RapidOCRProvider()
    files = sorted(Path(folder).glob("*.png"))
    imgs = [Image.open(f).convert("RGB") for f in files]
    t_first0 = time.perf_counter()
    prov.recognize([imgs[0]])
    cold = time.perf_counter() - t0
    warm = []
    for im in imgs[1:]:
        t = time.perf_counter()
        prov.recognize([im])
        warm.append((time.perf_counter() - t) * 1000)
    warm.sort()
    pick = lambda q: warm[min(len(warm) - 1, int(q * (len(warm) - 1) + 0.5))]  # noqa: E731
    print(json.dumps({"engine": engine, "version": prov.version, "cold_start_s": round(cold, 2),
                      "first_page_s": round(time.perf_counter() - t_first0, 2), "pages_timed": len(warm),
                      "warm_ms_p50": round(pick(0.5)), "warm_ms_p95": round(pick(0.95)),
                      "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}))


def model_size_mb(engine: str) -> float | None:
    try:
        if engine == "rapidocr":
            import rapidocr_onnxruntime
            root = Path(rapidocr_onnxruntime.__file__).parent
            return round(sum(f.stat().st_size for f in root.rglob("*.onnx")) / 1e6, 1)
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
        d = Path("/usr/share/tesseract-ocr")
        files = [f for f in d.rglob("eng.traineddata")] if d.exists() else []
        return round(sum(f.stat().st_size for f in files) / 1e6, 1) if files else None
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="benchmarks/results")
    ap.add_argument("--child", nargs=2)
    a = ap.parse_args()
    if a.child:
        child(*a.child)
        return
    from benchmarks.synthetic import generate
    folder = Path(tempfile.mkdtemp())
    for s in generate(a.n, seed=a.seed, degradations=("clean",), multipage_every=0):
        (folder / f"{s.id}.png").write_bytes(s.images[0])
    rep = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": "resources", "synthetic": True, "pages": a.n,
           "cpus": os.cpu_count(), "load_avg_start": os.getloadavg(), "engines": {}}
    for eng in ("tesseract", "rapidocr"):
        r = subprocess.run([sys.executable, "-m", "benchmarks.resources", "--child", eng, str(folder)],
                           capture_output=True, text=True, timeout=1200)
        rep["engines"][eng] = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"error": r.stderr[-200:]}
        rep["engines"][eng]["model_files_mb"] = model_size_mb(eng)
    rep["load_avg_end"] = os.getloadavg()
    body = json.dumps(rep, indent=1, sort_keys=True)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    path = out / f"resources-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{hashlib.sha256(body.encode()).hexdigest()[:10]}.json"
    path.write_text(body)
    print(body)


if __name__ == "__main__":
    main()
