"""End-to-end smoke test against a RUNNING stack (API + Celery worker + Postgres + Redis + OCR engine).
   python scripts/e2e_smoke.py http://localhost:8000 <admin_token>"""
import io
import sys
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

LINES = ["Himalayan Traders Pvt Ltd", "Invoice No: INV-2026-0042", "Date: 15/03/2026", "",
         "Description        Qty      Rate       Amount", "Rice 25kg            2    1,500.00     3,000.00",
         "Cooking Oil          1    1,200.00     1,200.00", "", "Sub Total                        4,200.00",
         "VAT 13%                            546.00", "Grand Total NPR                  4,746.00"]


def invoice_png() -> bytes:
    img = Image.new("RGB", (1100, 620), "white"); d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 32)
    except OSError:
        f = ImageFont.load_default()
    for i, t in enumerate(LINES):
        d.text((40, 30 + i * 50), t, fill="black", font=f)
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()


def main(base: str, admin: str) -> None:
    h = {"X-Admin-Token": admin}
    c = httpx.post(f"{base}/internal/v1/clients", json={"name": "smoke", "plan_id": "pro"}, headers=h).json()
    key = httpx.post(f"{base}/internal/v1/clients/{c['client_id']}/api-keys", json={"name": "smoke"}, headers=h).json()["api_key"]
    kh = {"X-API-Key": key}
    r = httpx.post(f"{base}/api/v1/bills", files={"file": ("inv.png", invoice_png(), "image/png")}, headers=kh)
    print("submit:", r.status_code, r.json())
    pid = r.json()["process_id"]
    for _ in range(60):
        body = httpx.get(f"{base}/api/v1/bills/{pid}", headers=kh).json()
        if body["status"] in ("completed", "failed"):
            break
        time.sleep(1)
    print("final:", body)
    fb = httpx.post(f"{base}/api/v1/bills/{pid}/feedback", json={"corrections": [{"field": "total", "value": 4700}]}, headers=kh)
    print("feedback:", fb.status_code, fb.json())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
