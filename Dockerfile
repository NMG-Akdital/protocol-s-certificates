FROM python:3.12-slim-bookworm

# WeasyPrint needs Pango/HarfBuzz; zbar is only for the acceptance test's QR decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libffi8 libzbar0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 1000 appuser && mkdir -p /data && chown appuser /data
USER appuser

ENV APP_ENV=production \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:${PORT:-8000}/healthz || exit 1
# Hosting platforms (e.g. Render) set $PORT; 8000 otherwise.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --proxy-headers --forwarded-allow-ips '*'"]
