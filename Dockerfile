# --- Build Stage ---
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# --- Final Production Runtime ---
FROM python:3.12-slim AS runtime

# Run as a non-root user. Created first so we can install deps into its home
# (a --user install under /root/.local is unreadable by a non-root process).
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Bring the isolated dependencies into the runtime user's home.
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local
COPY --chown=appuser:appuser ./app ./app

ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV ENV=production
# Pin tiktoken's cache so the encoding is resolved at build time. Otherwise the
# first telemetry write tries to download the BPE over the network, which
# silently disables token measurement in locked-down/offline environments.
ENV TIKTOKEN_CACHE_DIR=/home/appuser/.tiktoken_cache

USER appuser

RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:combined_app", "--host", "0.0.0.0", "--port", "8000"]
