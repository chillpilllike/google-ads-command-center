FROM python:3.12-slim AS source

ARG REPO_URL=https://github.com/chillpilllike/google-ads-command-center.git
ARG GIT_BRANCH=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Bust Docker cache whenever the public GitHub main branch changes. Use the
# public commits feed instead of api.github.com because Coolify builds can fail
# hard when the API endpoint returns a transient 5xx.
ADD https://github.com/chillpilllike/google-ads-command-center/commits/main.atom /tmp/github-version.xml

WORKDIR /src
RUN git clone --depth 1 --branch "$GIT_BRANCH" "$REPO_URL" .


FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_INSTANCE_ROLE=primary \
    PUBLIC_BASE_URL=https://googleads.gofinch.com \
    SESSION_COOKIE_SECURE=false \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    DATABASE_URL=change-this-in-private-dockerfile-or-coolify \
    POSTGRES_URL= \
    SECRET_KEY=change-this-in-private-dockerfile-or-coolify \
    ADMIN_EMAIL=admin \
    ADMIN_PASSWORD=change-this-in-private-dockerfile-or-coolify \
    DRAMATIQ_ENABLED=true \
    DRAMATIQ_PROCESSES=1 \
    DRAMATIQ_THREADS=2 \
    SCHEDULER_ENABLED=true \
    AUTOMATION_SCHEDULER_INTERVAL_SECONDS=900 \
    AUTOMATION_SCHEDULER_RECOMPUTE_EVERY_RUNS=0 \
    INIT_DRAMATIQ_SCHEMA=true \
    INIT_APP_DB=true \
    AUTO_INIT_DB=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=source /src/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=source /src/app ./app
COPY --from=source /src/browser_extension ./browser_extension
COPY --from=source /src/scripts ./scripts
COPY --from=source /src/config ./config
COPY --from=source /src/README.md ./

RUN chmod +x scripts/docker_start.sh scripts/run_automation_scheduler_loop.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/healthz', timeout=5).read()"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["scripts/docker_start.sh"]
