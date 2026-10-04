# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS deps
COPY requirements.txt constraints.txt ./
RUN pip install --prefix=/install -c constraints.txt -r requirements.txt

# --- API image: no OCR engine, no tesseract attack surface
FROM base AS api
COPY --from=deps /install /usr/local
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
# Operator scripts (bootstrap_client, e2e_smoke) run inside the container in development.
COPY scripts ./scripts
RUN useradd --system --uid 10001 --no-create-home appuser
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]

# --- Invoice review web layer: static pages + a small proxy. No database, storage or queue access; holds only the collection API key.
FROM base AS webui
COPY --from=deps /install /usr/local
COPY app ./app
RUN useradd --system --uid 10001 --no-create-home appuser
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "app.webui.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header"]

# --- Worker image: adds the OCR engine; runs with no inbound ports, read-only rootfs (see compose)
FROM base AS worker
# Celery prefork children keep metrics in their own memory; the multiprocess client aggregates via files here
# (must be set before Python starts; /tmp is a tmpfs in compose).
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prom
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deps /install /usr/local
COPY requirements-ocr-tesseract.txt constraints.txt ./
RUN pip install -c constraints.txt -r requirements-ocr-tesseract.txt
COPY app ./app
RUN useradd --system --uid 10001 --no-create-home appuser
USER 10001
# Queue selection is set per service in compose (-Q ocr | -Q ocr_bulk) so bulk capacity is isolated.
# Worker metrics are exposed on :9100 (internal network only; never publish this port).
CMD ["celery", "-A", "app.workers.celery_app", "worker", "-Q", "ocr", "--concurrency=2", "--loglevel=INFO", "--max-tasks-per-child=200"]
