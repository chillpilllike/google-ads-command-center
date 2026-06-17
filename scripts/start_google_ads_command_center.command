#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/Users/amitsoni/Documents/New project 16"
WEB_SESSION="google_ads_app_8010"
WORKER_SESSION="google_ads_worker"
PORT="8010"
URL="http://127.0.0.1:${PORT}/"
LOCK_DIR="/tmp/google_ads_command_center_launcher.lock"

cd "$APP_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  for _ in {1..10}; do
    sleep 1
    if curl -fsS "${URL}healthz" >/dev/null 2>&1; then
      open "$URL"
      exit 0
    fi
  done
  echo "Another launcher is already starting the app. Opening the portal."
  open "$URL"
  exit 0
fi

trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

screen_exists() {
  screen -ls | grep -Eq "[[:space:]][0-9]+[.]${1}[[:space:]]"
}

port_listening() {
  lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

app_process_exists() {
  local pattern="$1"
  local pid cwd
  for pid in $(pgrep -f "$pattern" 2>/dev/null || true); do
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    if [ "$cwd" = "$APP_DIR" ]; then
      return 0
    fi
  done
  return 1
}

if ! screen_exists "$WEB_SESSION"; then
  if port_listening; then
    echo "Port ${PORT} is already in use; opening existing app."
  else
    screen -dmS "$WEB_SESSION" scripts/run_web_8010.sh
  fi
fi

if ! app_process_exists ".venv/bin/dramatiq app.tasks"; then
  screen -dmS "$WORKER_SESSION" .venv/bin/dramatiq app.tasks --processes 1 --threads 1
fi

for _ in {1..25}; do
  if curl -fsS "${URL}healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

open "$URL"
