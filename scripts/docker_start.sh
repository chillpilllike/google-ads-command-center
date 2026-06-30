#!/usr/bin/env bash
set -euo pipefail

if [ -z "${DATABASE_URL:-}" ] && [ -n "${POSTGRES_URL:-}" ]; then
  export DATABASE_URL="${POSTGRES_URL}"
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL or POSTGRES_URL must be set. Store service credentials inside Postgres settings, not in the image." >&2
  exit 1
fi

APP_INSTANCE_ROLE="$(printf '%s' "${APP_INSTANCE_ROLE:-developer}" | tr '[:upper:]' '[:lower:]')"
export APP_INSTANCE_ROLE
if [ "$APP_INSTANCE_ROLE" != "primary" ]; then
  export GOOGLE_ADS_ALLOW_MUTATIONS=false
  export GOOGLE_ADS_DRY_RUN=true
  export DRAMATIQ_ENABLED=false
  export SCHEDULER_ENABLED=false
fi

mkdir -p state reports

pids=()

shutdown() {
  for pid in "${pids[@]:-}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  wait || true
}

trap shutdown TERM INT

uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --proxy-headers &
pids+=("$!")

if [ "${INIT_DRAMATIQ_SCHEMA:-true}" != "false" ] && [ "${INIT_DRAMATIQ_SCHEMA:-true}" != "0" ]; then
  python scripts/init_dramatiq_pg.py
fi

if [ "${INIT_APP_DB:-true}" != "false" ] && [ "${INIT_APP_DB:-true}" != "0" ]; then
  python scripts/init_app_db.py
fi

if [ "${DRAMATIQ_ENABLED:-true}" != "false" ] && [ "${DRAMATIQ_ENABLED:-true}" != "0" ]; then
  dramatiq app.tasks --processes "${DRAMATIQ_PROCESSES:-1}" --threads "${DRAMATIQ_THREADS:-2}" &
  pids+=("$!")
fi

if [ "${SCHEDULER_ENABLED:-true}" != "false" ] && [ "${SCHEDULER_ENABLED:-true}" != "0" ]; then
  scripts/run_automation_scheduler_loop.sh &
  pids+=("$!")
fi

set +e
wait -n "${pids[@]}"
status=$?
set -e
shutdown
exit "$status"
