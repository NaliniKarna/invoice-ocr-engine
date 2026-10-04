"""Load-test scenarios for the Bill OCR API (Locust). No throughput numbers are claimed anywhere in this repo
until this has been run against a production-like stack; see docs/CAPACITY.md.

Run against a disposable stack only (it creates real processes and consumes quota):
    export OCR_LOAD_API_KEY=<key of a dedicated load-test tenant on a high-limit plan>
    locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 20 -r 2 -t 5m --csv out/run1

Scenarios: interactive upload + poll, idempotent retry, and bulk upload. Responses of 429/503 are recorded as
expected backpressure, not failures. The quantities to read are p95/p99 latency of accepted uploads, the 429/503
share, and end-to-end time until a process is completed.
"""
import io
import os
import random
import time

from locust import HttpUser, between, task
from PIL import Image

KEY = os.environ.get("OCR_LOAD_API_KEY", "")


def unique_png() -> bytes:
    img = Image.new("RGB", (400, 300), "white")
    img.putpixel((random.randrange(400), random.randrange(300)), (random.randrange(256), 0, 0))  # unique sha256
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Interactive(HttpUser):
    weight = 8
    wait_time = between(0.5, 2)

    def on_start(self):
        self.client.headers["X-API-Key"] = KEY

    @task(5)
    def upload_and_poll(self):
        with self.client.post("/api/v1/bills", files={"file": ("bill.png", unique_png(), "image/png")},
                              catch_response=True, name="POST /bills") as r:
            if r.status_code in (429, 503):
                r.success()          # backpressure working as designed
                return
            if r.status_code != 202:
                r.failure(f"unexpected {r.status_code}")
                return
            pid = r.json()["process_id"]
        start = time.time()
        while time.time() - start < 60:
            g = self.client.get(f"/api/v1/bills/{pid}", name="GET /bills/{id}")
            if g.status_code == 200 and g.json()["status"] in ("completed", "failed"):
                return
            time.sleep(1)

    @task(1)
    def idempotent_retry(self):
        key = f"load-{random.getrandbits(64):016x}"
        data = unique_png()
        for _ in range(2):
            self.client.post("/api/v1/bills", files={"file": ("bill.png", data, "image/png")},
                             headers={"Idempotency-Key": key}, name="POST /bills (idempotent retry)")


class Bulk(HttpUser):
    weight = 1
    wait_time = between(5, 15)

    def on_start(self):
        self.client.headers["X-API-Key"] = KEY

    @task
    def bulk(self):
        files = [("files", (f"b{i}.png", unique_png(), "image/png")) for i in range(10)]
        with self.client.post("/api/v1/bills/bulk", files=files, catch_response=True, name="POST /bills/bulk") as r:
            if r.status_code in (202, 429, 503):
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")
