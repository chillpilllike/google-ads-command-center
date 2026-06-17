#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
INTERVAL_SECONDS="${AUTOMATION_SCHEDULER_INTERVAL_SECONDS:-900}"
RECOMPUTE_EVERY_RUNS="${AUTOMATION_SCHEDULER_RECOMPUTE_EVERY_RUNS:-4}"
LOG_FILE="${AUTOMATION_SCHEDULER_LOG:-${APP_DIR}/state/automation_scheduler.log}"

cd "$APP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

run_count=0
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
      .venv/bin/python scripts/queue_automation_monitor.py "${args[@]}"
    else
      .venv/bin/python scripts/queue_automation_monitor.py
    fi
  } >>"$LOG_FILE" 2>&1 || true

  sleep "$INTERVAL_SECONDS"
done
