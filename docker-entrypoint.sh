#!/bin/sh
set -eu

mkdir -p /data
if [ -n "${ARBOT_SETTINGS_PATH:-}" ] && [ ! -f "$ARBOT_SETTINGS_PATH" ]; then
  cp /app/config/settings.yaml "$ARBOT_SETTINGS_PATH"
fi

# Coolify/custom start command replaces CMD and is passed through.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

PROCESS="${ARBOT_PROCESS:-all}"

run_dashboard() {
  exec gunicorn \
    --bind "0.0.0.0:${PORT:-8788}" \
    --workers 1 \
    --threads 8 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    arbot.dashboard.app:app
}

case "$PROCESS" in
  dashboard)
    run_dashboard
    ;;
  bot)
    exec arbot run
    ;;
  all)
    arbot run &
    run_dashboard
    ;;
  *)
    echo "Unknown ARBOT_PROCESS=$PROCESS (use dashboard, bot, or all)" >&2
    exit 1
    ;;
esac
