#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_INSTANCE_ROLE="${APP_INSTANCE_ROLE:-developer}"
INTERVAL_SECONDS="${AUTOMATION_SCHEDULER_INTERVAL_SECONDS:-900}"
RECOMPUTE_EVERY_RUNS="${AUTOMATION_SCHEDULER_RECOMPUTE_EVERY_RUNS:-4}"
LOG_FILE="${AUTOMATION_SCHEDULER_LOG:-${APP_DIR}/state/automation_scheduler.log}"

cd "$APP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

case "$(printf '%s' "$APP_INSTANCE_ROLE" | tr '[:upper:]' '[:lower:]')" in
  primary) ;;
  *)
    echo "Automation scheduler disabled because APP_INSTANCE_ROLE=${APP_INSTANCE_ROLE}; only primary may queue live automation." >>"$LOG_FILE"
    exit 0
    ;;
esac

run_count=0
PYTHON_BIN="${PYTHON_BIN:-python}"
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi
while true; do
  run_count=$((run_count + 1))
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  args=()
  arg_count=0
  if [ "$RECOMPUTE_EVERY_RUNS" -gt 0 ] && [ $((run_count % RECOMPUTE_EVERY_RUNS)) -eq 1 ]; then
    args+=(--recompute-schedule)
    arg_count=1
  fi
  arg_label="none"
  if [ "$arg_count" -gt 0 ]; then
    arg_label="${args[*]}"
  fi

  {
    echo "[$timestamp] scheduler tick args=${arg_label}"
    if [ "$arg_count" -gt 0 ]; then
      "$PYTHON_BIN" scripts/queue_automation_monitor.py "${args[@]}"
    else
      "$PYTHON_BIN" scripts/queue_automation_monitor.py
    fi
  } >>"$LOG_FILE" 2>&1 || true

  sleep "$INTERVAL_SECONDS"
done
