#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUTPUT="$ROOT/e2e/test-results/provider-accounts"
mkdir -p "$OUTPUT"
export SQLITE_PATH="$OUTPUT/db.sqlite3"
export E2E_PROVIDER_STATE="$OUTPUT/state.json"
export PYTHONPATH="$ROOT/backend:$ROOT/e2e/fixtures${PYTHONPATH:+:$PYTHONPATH}"
export DJANGO_SETTINGS_MODULE=config.settings.development
for port in 8001 5174 8099; do
  if (echo >"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1; then
    echo "Port $port is occupied; stop its local test server first." >&2
    exit 1
  fi
done
rm -f "$SQLITE_PATH" "$SQLITE_PATH-wal" "$SQLITE_PATH-shm" "$OUTPUT/requests.jsonl" "$E2E_PROVIDER_STATE"
backend/.venv/bin/python backend/manage.py migrate --noinput >"$OUTPUT/migrations.log" 2>&1
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill -- "-$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM
setsid backend/.venv/bin/python e2e/fixtures/provider_accounts_fixture.py >"$OUTPUT/provider.log" 2>&1 & pids+=("$!")
setsid backend/.venv/bin/uvicorn provider_accounts_asgi:application --host 127.0.0.1 --port 8001 --lifespan on >"$OUTPUT/backend.log" 2>&1 & pids+=("$!")
setsid npm --prefix webapp run dev -- --host 127.0.0.1 --port 5174 --strictPort --config vite.focused-e2e.config.ts >"$OUTPUT/frontend.log" 2>&1 & pids+=("$!")
for _ in $(seq 1 60); do
  if [[ -f "$E2E_PROVIDER_STATE" ]] && curl -fsS http://127.0.0.1:8099/v1/models >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8001/api/v1/health/ >/dev/null 2>&1 && curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8099/v1/models >/dev/null
cd e2e
npx playwright test -c provider-accounts.config.ts "$@"
