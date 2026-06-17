FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    DRAMATIQ_ENABLED=true \
    DRAMATIQ_PROCESSES=1 \
    DRAMATIQ_THREADS=2 \
    SCHEDULER_ENABLED=true \
    AUTOMATION_SCHEDULER_INTERVAL_SECONDS=900 \
    AUTOMATION_SCHEDULER_RECOMPUTE_EVERY_RUNS=4 \
    INIT_DRAMATIQ_SCHEMA=true \
    INIT_APP_DB=true \
    AUTO_INIT_DB=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config ./config
COPY README.md ./

RUN chmod +x scripts/docker_start.sh scripts/run_automation_scheduler_loop.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/healthz', timeout=5).read()"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["scripts/docker_start.sh"]
