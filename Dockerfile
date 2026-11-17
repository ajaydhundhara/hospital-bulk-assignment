FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /srv

# Dependencies first, so code changes don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# The fake upstream lets `docker compose --profile fake up` run with no network access.
COPY scripts ./scripts
COPY samples ./samples

# Code is read-only for the app user, whatever modes the files had on the build host.
RUN chmod -R a+rX,go-w /srv \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/', timeout=4)"

# Single worker on purpose: job state, locks and the upstream limiter are per process
# (see DESIGN.md §7 and O8). Render provides $PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
