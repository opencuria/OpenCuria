#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_INSTALL_DIR="/opt/opencuria/runner"
DEFAULT_ENV_FILE="/etc/opencuria/runner.env"
DEFAULT_SYSTEMD_UNIT_DIR="/etc/systemd/system"
SERVICE_NAME="opencuria-runner.service"

INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
ENV_FILE="${DEFAULT_ENV_FILE}"
SYSTEMD_UNIT_DIR="${DEFAULT_SYSTEMD_UNIT_DIR}"
ENABLE_SERVICE=1
START_SERVICE=0
SKIP_APT=0

usage() {
  cat <<'EOF'
Usage: setup-native-qemu-runner.sh [options]

Prepare a Linux host for the native OpenCuria QEMU/KVM runner.

Options:
  --install-dir PATH         Install runner files into PATH
  --env-file PATH            Write/read runner env file at PATH
  --systemd-unit-dir PATH    Install the systemd unit into PATH
  --skip-apt                 Skip apt package installation
  --no-enable                Do not enable the systemd service
  --start                    Start or restart the service when config is ready
  -h, --help                 Show this help text
EOF
}

log() {
  printf '[opencuria-qemu-setup] %s\n' "$*"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    printf 'This script must run as root.\n' >&2
    exit 1
  fi
}

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "${cmd}" >&2
    exit 1
  fi
}

install_packages() {
  if [[ "${SKIP_APT}" -eq 1 ]]; then
    log "Skipping apt package installation"
    return
  fi

  require_command apt-get

  log "Installing host packages for QEMU/KVM runner"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    genisoimage \
    git \
    libvirt-clients \
    libvirt-daemon-system \
    libvirt-dev \
    openssh-client \
    pkg-config \
    python3 \
    python3-dev \
    python3-venv \
    qemu-system-x86 \
    qemu-utils \
    rsync \
    virtinst
}

install_runner_files() {
  require_command rsync

  log "Syncing runner files into ${INSTALL_DIR}"
  install -d -m 755 "${INSTALL_DIR}"
  rsync -a --delete \
    --exclude '.venv' \
    --exclude '.pytest_cache' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    "${RUNNER_DIR}/" "${INSTALL_DIR}/"
}

setup_virtualenv() {
  require_command python3

  log "Creating or updating Python virtualenv"
  if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${INSTALL_DIR}/.venv"
  fi

  "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
  "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
}

ensure_group_membership() {
  if getent group libvirt >/dev/null 2>&1; then
    usermod -a -G libvirt root || true
  fi
  if getent group kvm >/dev/null 2>&1; then
    usermod -a -G kvm root || true
  fi
}

ensure_directories() {
  log "Ensuring runner state directories exist with libvirt-safe permissions"
  install -d -o root -g root -m 755 /var/lib/opencuria
  install -d -o root -g root -m 755 /var/lib/opencuria/images
  install -d -o root -g root -m 755 /var/lib/opencuria/disks
  install -d -o root -g root -m 755 /var/lib/opencuria/snapshots
  install -d -o root -g root -m 755 /var/lib/opencuria/base-images
  install -d -o root -g root -m 700 /root/.local/share/opencuria
  install -d -o root -g root -m 700 /root/.local/share/opencuria/ssh
  install -d -o root -g root -m 755 /etc/opencuria
}

install_env_file() {
  local tmp
  tmp="$(mktemp)"
  cp "${INSTALL_DIR}/.env.example" "${tmp}"
  sed -i \
    -e 's/^RUNNER_ENABLED_RUNTIMES=.*/RUNNER_ENABLED_RUNTIMES=qemu/' \
    -e 's|^RUNNER_BACKEND_URL=.*|RUNNER_BACKEND_URL=http://localhost:8000|' \
    "${tmp}"

  if [[ ! -f "${ENV_FILE}" ]]; then
    log "Installing initial env file at ${ENV_FILE}"
    install -D -o root -g root -m 600 "${tmp}" "${ENV_FILE}"
  else
    log "Keeping existing env file at ${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  fi

  rm -f "${tmp}"
}

install_systemd_unit() {
  local unit_target="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}"

  log "Installing systemd unit to ${unit_target}"
  install -d -m 755 "${SYSTEMD_UNIT_DIR}"
  install -o root -g root -m 644 \
    "${INSTALL_DIR}/systemd/opencuria-runner.service" \
    "${unit_target}"
  systemctl daemon-reload

  if [[ "${ENABLE_SERVICE}" -eq 1 ]]; then
    systemctl enable "${SERVICE_NAME}"
  fi
}

ensure_services_running() {
  log "Ensuring libvirt service is enabled and running"
  systemctl enable --now libvirtd
}

env_value() {
  local key="$1"
  local value
  value="$(awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, "", $0); print $0}' "${ENV_FILE}" | tail -n1)"
  printf '%s' "${value}"
}

config_ready() {
  local api_token runtimes
  api_token="$(env_value RUNNER_API_TOKEN)"
  runtimes="$(env_value RUNNER_ENABLED_RUNTIMES)"

  [[ -n "${api_token}" ]] || return 1
  [[ "${runtimes}" == *qemu* ]] || return 1
}

maybe_start_service() {
  if [[ "${START_SERVICE}" -ne 1 ]]; then
    log "Service start skipped; rerun with --start after reviewing ${ENV_FILE}"
    return
  fi

  if ! config_ready; then
    log "Config in ${ENV_FILE} is not ready yet; not starting service"
    log "Set at least RUNNER_API_TOKEN, RUNNER_BACKEND_URL and keep RUNNER_ENABLED_RUNTIMES including qemu"
    return
  fi

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log "Restarting ${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
  else
    log "Starting ${SERVICE_NAME}"
    systemctl start "${SERVICE_NAME}"
  fi
}

print_next_steps() {
  cat <<EOF

Setup complete.

Files:
  runner install dir: ${INSTALL_DIR}
  runner env file:    ${ENV_FILE}
  systemd unit:       ${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}

Next steps:
  1. Edit ${ENV_FILE} and set RUNNER_API_TOKEN plus RUNNER_BACKEND_URL.
  2. Optionally change QEMU defaults such as memory, disk size and network.
  3. Start the runner with:
     systemctl start ${SERVICE_NAME}
  4. Check status/logs with:
     systemctl status ${SERVICE_NAME}
     journalctl -u ${SERVICE_NAME} -f
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --systemd-unit-dir)
      SYSTEMD_UNIT_DIR="$2"
      shift 2
      ;;
    --skip-apt)
      SKIP_APT=1
      shift
      ;;
    --no-enable)
      ENABLE_SERVICE=0
      shift
      ;;
    --start)
      START_SERVICE=1
      shift
      ;;
    -h|--help)
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

require_root
require_command systemctl
require_command install

install_packages
ensure_group_membership
ensure_directories
install_runner_files
setup_virtualenv
install_env_file
install_systemd_unit
ensure_services_running
maybe_start_service
print_next_steps
