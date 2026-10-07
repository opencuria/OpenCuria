#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${BACKEND_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  ENV_FILE="${BACKEND_DIR}/../.env"
fi
OUTPUT_DIR="/etc/systemd/system"
BACKEND_SERVICE="opencuria-backend.service"
RECOVERY_SERVICE="opencuria-recover-lifecycle.service"
RUNNER_SERVICE=""
activate=1

usage() {
  cat <<'EOF'
Usage:
  sudo ./backend/systemd/install-recovery-service.sh [OPTIONS]

Installs and enables the recovery worker, applies migrations, and restarts the
backend and worker together. The backend starts the worker through a Wants
dependency; systemd monitors successful recovery ticks with a watchdog.

Options:
  --env-file PATH         Backend environment file (backend/.env or ../.env).
  --backend-service NAME  Backend systemd unit (opencuria-backend.service).
  --recovery-service NAME Recovery systemd unit (opencuria-recover-lifecycle.service).
  --restart-runner NAME   Also restart this local runner to load updated code.
  --output-dir PATH       Unit destination (/etc/systemd/system).
  --no-activate           Render files only; do not migrate or control services.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?--env-file requires a path}"; shift 2 ;;
    --backend-service) BACKEND_SERVICE="${2:?--backend-service requires a name}"; shift 2 ;;
    --recovery-service) RECOVERY_SERVICE="${2:?--recovery-service requires a name}"; shift 2 ;;
    --restart-runner) RUNNER_SERVICE="${2:?--restart-runner requires a name}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?--output-dir requires a path}"; shift 2 ;;
    --no-activate) activate=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

for unit in "${BACKEND_SERVICE}" "${RECOVERY_SERVICE}"; do
  if [[ ! "${unit}" =~ ^[a-zA-Z0-9_.@-]+\.service$ ]]; then
    printf 'Invalid systemd service name: %s\n' "${unit}" >&2
    exit 1
  fi
done
if [[ -n "${RUNNER_SERVICE}" && ! "${RUNNER_SERVICE}" =~ ^[a-zA-Z0-9_.@-]+\.service$ ]]; then
  printf 'Invalid runner service name: %s\n' "${RUNNER_SERVICE}" >&2
  exit 1
fi
if [[ "${BACKEND_SERVICE}" == "${RECOVERY_SERVICE}" ]]; then
  printf 'Backend and recovery must use distinct units.\n' >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" || ! -x "${BACKEND_DIR}/.venv/bin/python" ]]; then
  printf 'A backend environment file and virtualenv are required.\n' >&2
  exit 1
fi
if [[ "${activate}" -eq 1 && "${EUID}" -ne 0 ]]; then
  printf 'Run with sudo to install and activate system services.\n' >&2
  exit 1
fi

"${BACKEND_DIR}/.venv/bin/python" - \
  "${BACKEND_DIR}" "${ENV_FILE}" "${OUTPUT_DIR}" \
  "${BACKEND_SERVICE}" "${RECOVERY_SERVICE}" <<'PY'
import sys
from pathlib import Path

backend_dir, env_file, output_dir, backend_unit, recovery_unit = sys.argv[1:]
output = Path(output_dir)
output.mkdir(parents=True, exist_ok=True)

def escape_path(value: str) -> str:
    return str(Path(value).resolve()).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

template = (Path(backend_dir) / "systemd/opencuria-recovery.service").read_text()
unit = template.replace("__BACKEND_DIR__", escape_path(backend_dir))
unit = unit.replace("__WORKING_DIR__", str(Path(backend_dir).resolve()).replace("%", "%%"))
unit = unit.replace("__ENV_FILE__", str(Path(env_file).resolve()).replace("%", "%%"))
unit = unit.replace("__BACKEND_SERVICE__", backend_unit)
unit_path = output / recovery_unit
unit_path.write_text(unit)
unit_path.chmod(0o644)
dropin = output / (backend_unit + ".d") / "20-lifecycle-recovery.conf"
dropin.parent.mkdir(parents=True, exist_ok=True)
dropin.write_text(f"[Unit]\nWants={recovery_unit}\n")
dropin.chmod(0o644)
PY

printf 'Rendered recovery unit and backend dependency in %s\n' "${OUTPUT_DIR}"
if [[ "${activate}" -eq 0 ]]; then
  exit 0
fi

run_management() {
  "${BACKEND_DIR}/.venv/bin/python" - "${BACKEND_DIR}" "${ENV_FILE}" "$@" <<'PY'
import os
import subprocess
import sys
from pathlib import Path
from dotenv import dotenv_values

backend_dir, env_file, *arguments = sys.argv[1:]
environment = dict(os.environ)
environment.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
result = subprocess.run(
    [str(Path(backend_dir) / ".venv/bin/python"), "manage.py", *arguments],
    cwd=backend_dir,
    env=environment,
    check=False,
)
sys.exit(result.returncode)
PY
}

run_management check
systemctl daemon-reload
systemctl stop "${BACKEND_SERVICE}" "${RECOVERY_SERVICE}"
# Restore availability even if migration fails; keep the failure exit status.
trap 'systemctl start "${BACKEND_SERVICE}" "${RECOVERY_SERVICE}" || true' EXIT
run_management migrate --noinput
systemctl enable "${RECOVERY_SERVICE}"
systemctl start "${BACKEND_SERVICE}" "${RECOVERY_SERVICE}"
systemctl is-active --quiet "${BACKEND_SERVICE}"
systemctl is-active --quiet "${RECOVERY_SERVICE}"
if [[ -n "${RUNNER_SERVICE}" ]]; then
  systemctl restart "${RUNNER_SERVICE}"
  systemctl is-active --quiet "${RUNNER_SERVICE}"
fi
trap - EXIT
systemctl show "${RECOVERY_SERVICE}" \
  -p ActiveState -p SubState -p UnitFileState -p WatchdogUSec -p StatusText
