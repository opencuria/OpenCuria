#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${SCRIPT_DIR}/opencuria-runner.service"
OUTPUT_PATH="/etc/systemd/system/opencuria-runner.service"

usage() {
  cat <<'EOF'
Usage:
  sudo ./runner/systemd/install-runner-service.sh [--output PATH] [--no-reload]

Installs a rendered opencuria systemd service that points at this repository's
runner checkout and uses runner/.env directly.
EOF
}

reload_systemd=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      OUTPUT_PATH="${2:?--output requires a path}"
      shift 2
      ;;
    --no-reload)
      reload_systemd=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "${RUNNER_DIR}/.env" ]]; then
  printf 'Missing runner env file: %s\n' "${RUNNER_DIR}/.env" >&2
  printf 'Create it first with: cp %s/.env.example %s/.env\n' "${RUNNER_DIR}" "${RUNNER_DIR}" >&2
  exit 1
fi

if [[ ! -x "${RUNNER_DIR}/.venv/bin/python" ]]; then
  printf 'Missing runner virtualenv python: %s\n' "${RUNNER_DIR}/.venv/bin/python" >&2
  printf 'Set it up first inside %s\n' "${RUNNER_DIR}" >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Please run this installer with sudo so it can write %s\n' "${OUTPUT_PATH}" >&2
  exit 1
fi

escaped_runner_dir="$(printf '%s' "${RUNNER_DIR}" | sed 's/[&|]/\\&/g')"
rendered_unit="$(
  sed "s|__RUNNER_DIR__|${escaped_runner_dir}|g" "${TEMPLATE_PATH}"
)"

install -D -m 0644 /dev/stdin "${OUTPUT_PATH}" <<<"${rendered_unit}"

if [[ "${reload_systemd}" -eq 1 ]]; then
  systemctl daemon-reload
fi

printf 'Installed systemd unit at %s\n' "${OUTPUT_PATH}"
printf 'Runner checkout: %s\n' "${RUNNER_DIR}"
printf 'Next step: sudo systemctl enable --now %s\n' "$(basename "${OUTPUT_PATH}" .service)"
