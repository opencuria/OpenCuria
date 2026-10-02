#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_ID="${E2E_RUN_ID:-focused-$(date +%s)}"
DB_DIR="${E2E_DB_DIR:-$ROOT/e2e/test-results/focused-db}"
DB_PATH="$DB_DIR/plugin-e2e.sqlite3"
STATE="${E2E_FIXTURE_STATE:-$ROOT/e2e/test-results/focused-$RUN_ID.json}"
ARTIFACTS="${E2E_OUTPUT_DIR:-$ROOT/e2e/test-results/focused-$RUN_ID}"
mkdir -p "$DB_DIR" "$ARTIFACTS"

# Use an explicit seed only when requested; the default always starts with a
# new empty database and applies the current migrations.
if [[ -n "${E2E_SEED_DB:-}" ]]; then
  [[ -f "$E2E_SEED_DB" ]] || { echo "E2E_SEED_DB not found" >&2; exit 1; }
  cp "$E2E_SEED_DB" "$DB_PATH"
else
  rm -f "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm"
fi
export E2E_RUN_ID="$RUN_ID" E2E_FIXTURE_STATE="$STATE" SQLITE_PATH="$DB_PATH"
export E2E_OUTPUT_DIR="$ARTIFACTS"
export E2E_SCREENSHOTS_DIR="${E2E_SCREENSHOTS_DIR:-/workspace/.opencuria/playwright}"
export E2E_BASE_URL=http://127.0.0.1:5174 E2E_API_URL=http://127.0.0.1:5174/api/v1
export E2E_API_ORIGIN=http://127.0.0.1:8001
export MCP_OAUTH_CALLBACK_URL=http://127.0.0.1:8001/api/v1/mcp-oauth/callback/
export MCP_OAUTH_FRONTEND_RETURN_URL='http://127.0.0.1:5174/?settings=credentials'
export PYTHONPATH="$ROOT/backend:$ROOT/e2e/fixtures${PYTHONPATH:+:$PYTHONPATH}"
export DJANGO_SETTINGS_MODULE=config.settings
export DJANGO_ENV=development

cd "$ROOT/backend"
./.venv/bin/python manage.py migrate --noinput
cd "$ROOT"

# Isolate child trees so npm/Vite and Daphne descendants are always reaped.
setsid backend/.venv/bin/daphne -b 127.0.0.1 -p 8001 focused_asgi:application \
  >"$DB_DIR/../focused-$RUN_ID-api.log" 2>&1 &
API_PID=$!
setsid npm --prefix webapp run dev -- --host 127.0.0.1 --port 5174 --strictPort \
  --config vite.focused-e2e.config.ts >"$DB_DIR/../focused-$RUN_ID-web.log" 2>&1 &
WEB_PID=$!
cleanup_processes() {
  kill -- "-$WEB_PID" "-$API_PID" 2>/dev/null || true
  wait "$WEB_PID" "$API_PID" 2>/dev/null || true
  for port in 8001 5174; do
    for _ in $(seq 1 30); do
      if ! (echo >/dev/tcp/127.0.0.1/$port) >/dev/null 2>&1; then break; fi
      sleep 0.2
    done
  done
}
trap cleanup_processes EXIT INT TERM

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8001/api/v1/health/ >/dev/null 2>&1 && curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8001/api/v1/health/ >/dev/null
curl -fsS http://127.0.0.1:5174/ >/dev/null
cd "$ROOT/e2e"
npx playwright test -c playwright.focused.config.ts "$@"
