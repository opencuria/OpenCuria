#!/usr/bin/env bash
# OpenCuria installer: control plane (Docker Compose), QEMU runner (systemd) or both.
#
#   curl -fsSL https://opencuria.dev/install.sh | sudo bash
#
# opencuria.dev/install.sh redirects to this file on main (raw.githubusercontent.com).
# On a host that already runs OpenCuria it offers to update or to uninstall it.
# See --help for non-interactive use. Everything runs from main() so a truncated
# download never executes half a script.

set -Eeuo pipefail

REPO="opencuria/OpenCuria"
INSTALL_URL="https://opencuria.dev/install.sh"
LOCAL_BACKEND_URL="http://127.0.0.1:8000"

COMPOSE_PROJECT="opencuria"
COMPOSE_LABEL="label=com.docker.compose.project=${COMPOSE_PROJECT}"
DB_VOLUME="${COMPOSE_PROJECT}_pgdata"
OPENCURIA_IMAGES=(ghcr.io/opencuria/backend ghcr.io/opencuria/webapp)

RUNNER_SERVICE="opencuria-runner"
RUNNER_UNIT="/etc/systemd/system/${RUNNER_SERVICE}.service"
# Runner storage (runner/.env.example), the unit's StateDirectory and the home
# of root, where the runner keeps its SSH key for workspace VMs.
DATA_DIR="/var/lib/opencuria"
STATE_DIR="/var/lib/opencuria-runner"
LIBVIRT_URI="qemu:///system"
VM_PREFIX="opencuria-workspace-"
NETWORK_PREFIX="opencuria-ws-"
LIBVIRT_NETWORK_CONF="/etc/libvirt/network.conf"
LIBVIRT_PIN_COMMENT="# Set by the OpenCuria installer: works next to Docker."

# What the installer creates inside the installation directory.
INSTALL_ENTRIES=(.env .bootstrapped compose.yml Caddyfile secrets runner runner.new runner.old)
# Runner settings that may move storage out of the default directories.
RUNNER_PATH_KEYS=(RUNNER_QEMU_IMAGE_CACHE_DIR RUNNER_QEMU_DISK_DIR
  RUNNER_QEMU_SNAPSHOT_DIR RUNNER_STATE_DIR RUNNER_QEMU_SSH_KEY_PATH)

# Options
ACTION=""
DOMAIN=""
DOMAIN_SET=0
ADMIN_EMAIL=""
ADMIN_PASSWORD=""
PASSWORD_GENERATED=0
BACKEND_URL=""
RUNNER_TOKEN=""
INSTALL_DIR="/opt/opencuria"
DIR_SET=0
VERSION="latest"
VERSION_SET=0
ASSUME_YES=0

# Run state
MODE="" # components to install or update: full, server or runner
TTY=0
SRC_DIR=""
WORK_DIR=""
PUBLIC_URL=""
CURRENT_STEP="Preparing"
ROOT_SHARE_DIR=""
DATABASE_RESET=0
INSTALLED_VERSION="" # OPENCURIA_TAG of the existing installation

# Detected state (detect_files, detect_docker, detect_libvirt)
SERVER_ENV=0
SERVER_READY=0
SERVER_FILES=0
RUNNER_ENV=0
RUNNER_FILES=0
DOCKER_STATE="missing" # missing, down or up
CP_CONTAINERS=()
CP_RUNNING=0
CP_VOLUMES=()
CP_NETWORKS=()
CP_IMAGES=()
CP_DATA=0
LIBVIRT_STATE="missing" # missing, down or up
VM_DOMAINS=()
VM_RUNNING=0
VM_NETWORKS=()
LEFTOVER_PATHS=()

usage() {
  cat <<EOF
Install, update or uninstall OpenCuria on this host.

Usage:
  curl -fsSL ${INSTALL_URL} | sudo bash
  curl -fsSL ${INSTALL_URL} | sudo bash -s -- [ACTION] [OPTIONS]

Actions (asked interactively when omitted):
  full       Install the control plane and a runner on this host, wired together
  server     Install the control plane only: backend, web app, PostgreSQL, Redis,
             HTTPS proxy
  runner     Install a runner only, connected to an existing control plane
  update     Update what is installed; configuration and data are kept
  uninstall  Remove OpenCuria completely: workspaces, images, database, files

On a host that already runs OpenCuria the installer offers update and uninstall;
without a terminal it updates. Updates stay on the installed version (latest, dev
or a commit) unless --version or --dev selects another.

Options:
  --domain NAME          Public domain; HTTPS via Let's Encrypt (DNS must point
                         here, ports 80/443 open). An IP address or no domain
                         serves plain HTTP on port 80.
  --admin-email EMAIL    First admin account (new control planes)
  --admin-password PASS  Its password (default: generated and printed once)
  --url URL              Runner: control plane URL, e.g. https://opencuria.example.com
  --token TOKEN          Runner: token from Settings > Runners > Register Runner
  --dir PATH             Installation directory (default: /opt/opencuria)
  --version VERSION      Image tag / commit to install (default: the installed
                         version, else latest)
  --dev                  Follow the development branch (same as --version dev)
  -y, --yes              Never prompt; fail when a required value is missing.
                         Required to uninstall without a terminal.
  -h, --help             Show this help
EOF
}

# -- output ----------------------------------------------------------------

if [[ -t 2 ]]; then
  C_BOLD=$'\033[1m' C_RED=$'\033[31m' C_YELLOW=$'\033[33m' C_GREEN=$'\033[32m'
  C_RESET=$'\033[0m'
else
  C_BOLD="" C_RED="" C_YELLOW="" C_GREEN="" C_RESET=""
fi

step() {
  CURRENT_STEP="$*"
  printf '\n%s==> %s%s\n' "${C_BOLD}" "$*" "${C_RESET}" >&2
}
info() { printf '    %s\n' "$*" >&2; }
warn() { printf '%sWarning:%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die() {
  printf '%sError:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2
  exit 1
}

# Every command that fails unexpectedly ends the run with this message instead
# of a silent exit. Subshells leave the report to the main shell.
on_error() {
  [[ "${BASHPID}" -eq "$$" ]] || exit "$1"
  trap - ERR
  printf '%sError:%s %s failed: "%s" exited with status %s (install.sh line %s).\n' \
    "${C_RED}" "${C_RESET}" "${CURRENT_STEP}" "$3" "$1" "$2" >&2
  printf 'Fix the cause and run the installer again; it continues from the current state.\n' >&2
  exit "$1"
}

on_interrupt() {
  trap - ERR
  printf '\n' >&2
  die "Interrupted during: ${CURRENT_STEP}. Run the installer again to continue."
}

# -- prompts (read from the terminal: stdin is the script under curl | bash) --

# ask VAR PROMPT [DEFAULT]
ask() {
  local _ask_reply="" _ask_prompt="$2"
  [[ -z "${3:-}" ]] || _ask_prompt+=" [$3]"
  if [[ "${TTY}" -eq 1 ]]; then
    read -r -p "${_ask_prompt}: " _ask_reply </dev/tty || true
  fi
  printf -v "$1" '%s' "${_ask_reply:-${3:-}}"
}

# ask_secret VAR PROMPT
ask_secret() {
  local _ask_reply=""
  if [[ "${TTY}" -eq 1 ]]; then
    read -r -s -p "$2: " _ask_reply </dev/tty || true
    printf '\n' >/dev/tty
  fi
  printf -v "$1" '%s' "${_ask_reply}"
}

# confirm PROMPT -> 0 for yes (default), 1 for no
confirm() {
  local reply=""
  [[ "${TTY}" -eq 1 ]] || return 0
  read -r -p "$1 [Y/n]: " reply </dev/tty || true
  [[ ! "${reply}" =~ ^[Nn] ]]
}

# -- helpers ---------------------------------------------------------------

random_string() { # random_string LENGTH, alphanumeric
  head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c "1-$1"
}

# Fernet key: URL-safe base64 of 32 random bytes.
fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_'; }

# Same shape as secrets.token_urlsafe(48), which the backend issues.
runner_token() { head -c 48 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }

# set_env_value FILE KEY VALUE: replace or append KEY=VALUE, keep permissions.
set_env_value() {
  local tmp
  tmp="$(mktemp)"
  awk -v key="$2" -v value="$3" '
    index($0, key "=") == 1 { if (!done) print key "=" value; done = 1; next }
    { print }
    END { if (!done) print key "=" value }
  ' "$1" >"${tmp}"
  cat "${tmp}" >"$1"
  rm -f "${tmp}"
}

env_value() { # env_value FILE KEY: last value of KEY, without surrounding quotes
  [[ -f "$1" ]] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# lines_to ARRAY TEXT: the non-empty lines of TEXT, deduplicated, into ARRAY.
lines_to() {
  local -n _lines_out="$1"
  local line
  _lines_out=()
  while IFS= read -r line; do
    [[ -n "${line}" && " ${_lines_out[*]-} " != *" ${line} "* ]] || continue
    _lines_out+=("${line}")
  done <<<"$2"
}

json_escape() {
  local s="${1//\\/\\\\}"
  printf '%s' "${s//\"/\\\"}"
}

detect_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')" || true
  [[ -n "${ip}" ]] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
  printf '%s' "${ip:-127.0.0.1}"
}

plural() { # plural COUNT WORD -> "1 VM", "2 VMs"
  if [[ "$1" -eq 1 ]]; then printf '%s %s' "$1" "$2"; else printf '%s %ss' "$1" "$2"; fi
}

compose() { (cd "${INSTALL_DIR}" && docker compose "$@"); }

cleanup() { [[ -z "${WORK_DIR}" ]] || rm -rf "${WORK_DIR}"; }

has_systemd() { command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; }

# -- Docker and libvirt ------------------------------------------------------

docker_ready() { timeout 30 docker info >/dev/null 2>&1; }

# start_docker ENABLE: start the daemon; with ENABLE=1 also at boot.
start_docker() {
  docker_ready && return 0
  has_systemd || die "Docker is not running. Start it and run the installer again."
  info "Starting Docker"
  local out deadline=$((SECONDS + 90))
  if [[ "$1" -eq 1 ]]; then
    out="$(systemctl enable --now docker.service 2>&1)" ||
      die "Docker is not running and could not be started: ${out}"
  else
    out="$(systemctl start docker.service 2>&1)" ||
      die "Docker is not running and could not be started: ${out}"
  fi
  until docker_ready; do
    ((SECONDS < deadline)) ||
      die "Docker did not become ready. Check: systemctl status docker; journalctl -u docker"
    sleep 2
  done
}

virsh_() { timeout 120 virsh -q -c "${LIBVIRT_URI}" "$@"; }

libvirt_ready() { timeout 30 virsh -q -c "${LIBVIRT_URI}" list --name >/dev/null 2>&1; }

start_libvirt() {
  libvirt_ready && return 0
  has_systemd || die "libvirt is not running. Start it and run the installer again."
  info "Starting libvirt"
  local unit=libvirtd out deadline=$((SECONDS + 90))
  if ! systemctl cat libvirtd.service >/dev/null 2>&1 &&
    systemctl cat virtqemud.service >/dev/null 2>&1; then
    unit=virtqemud
  fi
  out="$(systemctl start "${unit}.service" 2>&1)" ||
    die "libvirt is not running and could not be started: ${out}"
  until libvirt_ready; do
    ((SECONDS < deadline)) ||
      die "libvirt did not become ready. Check: systemctl status ${unit}"
    sleep 2
  done
}

# -- state detection -----------------------------------------------------------

# A runner unit written by an earlier run names its directory; use it when no
# --dir was passed and the default directory holds no installation.
resolve_install_dir() {
  [[ "${DIR_SET}" -eq 0 && -f "${RUNNER_UNIT}" ]] || return 0
  [[ ! -e "${INSTALL_DIR}/.env" && ! -d "${INSTALL_DIR}/runner" ]] || return 0
  local dir
  dir="$(sed -n 's/^WorkingDirectory=//p' "${RUNNER_UNIT}" | tail -n 1)"
  [[ "${dir}" == */runner ]] || return 0
  dir="${dir%/runner}"
  if [[ "${dir}" =~ ^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ && "${dir}" != "${INSTALL_DIR}" &&
    -d "${dir}" ]]; then
    INSTALL_DIR="${dir}"
    info "Found the installation in ${INSTALL_DIR}"
  fi
}

# Finish a runner directory swap that an interrupted run left behind.
recover_runner_swap() {
  local dir="${INSTALL_DIR}/runner"
  [[ ! -d "${dir}" ]] || return 0
  if [[ -f "${dir}.new/.env" ]]; then
    mv "${dir}.new" "${dir}"
  elif [[ -d "${dir}.old" ]]; then
    mv "${dir}.old" "${dir}"
  else
    return 0
  fi
  info "Restored ${dir} after an interrupted update"
}

detect_files() {
  SERVER_ENV=0 SERVER_READY=0 SERVER_FILES=0 RUNNER_ENV=0 RUNNER_FILES=0
  LEFTOVER_PATHS=()
  if [[ -f "${INSTALL_DIR}/.env" ]]; then SERVER_ENV=1; fi
  if [[ -f "${INSTALL_DIR}/.bootstrapped" ]]; then SERVER_READY=1; fi
  if [[ -e "${INSTALL_DIR}/compose.yml" || -e "${INSTALL_DIR}/Caddyfile" ||
    -e "${INSTALL_DIR}/.bootstrapped" || -d "${INSTALL_DIR}/secrets" ]]; then
    SERVER_FILES=1
  fi
  if [[ -f "${INSTALL_DIR}/runner/.env" ]]; then RUNNER_ENV=1; fi
  if [[ -d "${INSTALL_DIR}/runner" || -d "${INSTALL_DIR}/runner.new" ||
    -d "${INSTALL_DIR}/runner.old" || -e "${RUNNER_UNIT}" || -L "${RUNNER_UNIT}" ]]; then
    RUNNER_FILES=1
  fi
  local path
  for path in "${DATA_DIR}" "${STATE_DIR}" "${ROOT_SHARE_DIR}"; do
    if [[ -e "${path}" ]]; then LEFTOVER_PATHS+=("${path}"); fi
  done
}

detect_docker() {
  DOCKER_STATE=missing CP_CONTAINERS=() CP_RUNNING=0 CP_VOLUMES=() CP_NETWORKS=() CP_IMAGES=()
  CP_DATA=0
  command -v docker >/dev/null || return 0
  DOCKER_STATE=down
  docker_ready || return 0
  DOCKER_STATE=up
  local out running=() volume
  out="$(docker container ls -aq --filter "${COMPOSE_LABEL}")" ||
    die "Could not list Docker containers."
  lines_to CP_CONTAINERS "${out}"
  out="$(docker container ls -q --filter "${COMPOSE_LABEL}" --filter status=running)" ||
    die "Could not list Docker containers."
  lines_to running "${out}"
  CP_RUNNING="${#running[@]}"
  out="$(docker volume ls -q --filter "${COMPOSE_LABEL}")" || die "Could not list Docker volumes."
  # Also by name: a volume without compose labels still holds the data.
  for volume in pgdata redisdata backend-runtime caddy-data caddy-config; do
    if docker volume inspect "${COMPOSE_PROJECT}_${volume}" >/dev/null 2>&1; then
      out+=$'\n'"${COMPOSE_PROJECT}_${volume}"
    fi
  done
  lines_to CP_VOLUMES "${out}"
  if [[ " ${CP_VOLUMES[*]-} " == *" ${DB_VOLUME} "* ]]; then CP_DATA=1; fi
  out="$(docker network ls -q --filter "${COMPOSE_LABEL}")" || die "Could not list Docker networks."
  lines_to CP_NETWORKS "${out}"
  out="$(docker image ls -q --filter "reference=${OPENCURIA_IMAGES[0]}" \
    --filter "reference=${OPENCURIA_IMAGES[1]}")" || die "Could not list Docker images."
  lines_to CP_IMAGES "${out}"
}

detect_libvirt() {
  LIBVIRT_STATE=missing VM_DOMAINS=() VM_RUNNING=0 VM_NETWORKS=()
  command -v virsh >/dev/null || return 0
  LIBVIRT_STATE=down
  libvirt_ready || return 0
  LIBVIRT_STATE=up
  local out running=()
  out="$(virsh_ list --all --name)" || die "Could not list libvirt domains (virsh list)."
  lines_to VM_DOMAINS "$(grep "^${VM_PREFIX}" <<<"${out}" || true)"
  out="$(virsh_ list --state-running --name)" || die "Could not list libvirt domains (virsh list)."
  lines_to running "$(grep "^${VM_PREFIX}" <<<"${out}" || true)"
  VM_RUNNING="${#running[@]}"
  out="$(virsh_ net-list --all --name)" || die "Could not list libvirt networks (virsh net-list)."
  lines_to VM_NETWORKS "$(grep "^${NETWORK_PREFIX}" <<<"${out}" || true)"
}

server_installed() {
  [[ "${SERVER_ENV}" -eq 1 || "${SERVER_FILES}" -eq 1 || "${#CP_CONTAINERS[@]}" -gt 0 ||
    "${#CP_VOLUMES[@]}" -gt 0 ]]
}

runner_installed() { [[ "${RUNNER_ENV}" -eq 1 || "${RUNNER_FILES}" -eq 1 ]]; }

# Workspace VMs, networks or storage without an installed runner.
has_leftovers() {
  [[ "${#VM_DOMAINS[@]}" -gt 0 || "${#VM_NETWORKS[@]}" -gt 0 || "${#LEFTOVER_PATHS[@]}" -gt 0 ]]
}

server_status() {
  case "${DOCKER_STATE}" in
    missing) printf 'Docker is not installed' ;;
    down) printf 'Docker is not running' ;;
    up)
      if [[ "${#CP_CONTAINERS[@]}" -eq 0 ]]; then
        printf 'no containers'
      else
        printf '%s of %s running' "${CP_RUNNING}" "$(plural "${#CP_CONTAINERS[@]}" container)"
      fi
      ;;
  esac
}

runner_status() {
  local state
  has_systemd || { printf 'no systemd'; return 0; }
  if [[ ! -e "${RUNNER_UNIT}" ]]; then
    printf 'service missing'
    return 0
  fi
  state="$(systemctl is-active "${RUNNER_SERVICE}.service" 2>/dev/null)" || true
  printf 'service %s' "${state:-unknown}"
}

show_installation() {
  local url
  printf '\nOpenCuria is installed on this host:\n' >&2
  if server_installed; then
    url="$(env_value "${INSTALL_DIR}/.env" PUBLIC_URL)"
    info "Control plane  ${url:-${INSTALL_DIR}} ($(server_status))"
  fi
  if runner_installed; then
    url="$(env_value "${INSTALL_DIR}/runner/.env" RUNNER_BACKEND_URL)"
    info "Runner         ${INSTALL_DIR}/runner ($(runner_status)${url:+, control plane ${url}})"
  fi
  case "${LIBVIRT_STATE}" in
    up)
      if [[ "${#VM_DOMAINS[@]}" -gt 0 ]]; then
        info "Workspaces     $(plural "${#VM_DOMAINS[@]}" VM), ${VM_RUNNING} running"
      fi
      ;;
    down) runner_installed && info "Workspaces     unknown: libvirt is not running" ;;
  esac
  return 0
}

# -- arguments -------------------------------------------------------------------

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      full | server | runner | update | uninstall)
        [[ -z "${ACTION}" ]] || die "Pass only one action, not both ${ACTION} and $1."
        ACTION="$1"
        ;;
      --domain) DOMAIN="${2?--domain needs a value}" DOMAIN_SET=1; shift ;;
      --admin-email) ADMIN_EMAIL="${2?--admin-email needs a value}"; shift ;;
      --admin-password) ADMIN_PASSWORD="${2?--admin-password needs a value}"; shift ;;
      --url) BACKEND_URL="${2?--url needs a value}"; shift ;;
      --token) RUNNER_TOKEN="${2?--token needs a value}"; shift ;;
      --dir) INSTALL_DIR="${2?--dir needs a value}" DIR_SET=1; shift ;;
      --version) VERSION="${2?--version needs a value}" VERSION_SET=1; shift ;;
      --dev) VERSION=dev VERSION_SET=1 ;;
      -y | --yes) ASSUME_YES=1 ;;
      -h | --help) usage; exit 0 ;;
      *) die "Unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  [[ "${VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || die "Invalid --version: ${VERSION}"
  while [[ "${INSTALL_DIR}" == */ ]]; do INSTALL_DIR="${INSTALL_DIR%/}"; done
  # At least two path components: uninstall deletes the installer's files there.
  [[ "${INSTALL_DIR}" =~ ^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ && "${INSTALL_DIR}" != *..* ]] ||
    die "Invalid --dir: ${INSTALL_DIR} (use a dedicated directory such as /opt/opencuria)"
}

# -- action ----------------------------------------------------------------------

# An installation keeps its version (e.g. dev) unless --version or --dev is passed.
resolve_version() {
  INSTALLED_VERSION="$(env_value "${INSTALL_DIR}/.env" OPENCURIA_TAG)"
  [[ -n "${INSTALLED_VERSION}" ]] ||
    INSTALLED_VERSION="$(env_value "${INSTALL_DIR}/runner/.env" OPENCURIA_TAG)"
  [[ "${INSTALLED_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || INSTALLED_VERSION=""
  if [[ "${VERSION_SET}" -eq 0 && -n "${INSTALLED_VERSION}" ]]; then
    VERSION="${INSTALLED_VERSION}"
  fi
}

choose_action() {
  local choice
  if [[ -n "${ACTION}" ]]; then
    return 0
  fi
  if server_installed || runner_installed; then
    if [[ "${TTY}" -eq 0 ]]; then
      ACTION=update
      info "OpenCuria is installed; updating it (pass uninstall to remove it)."
      return 0
    fi
    show_installation
    local target="version ${VERSION}"
    case "${VERSION}" in
      latest) target="the latest version" ;;
      dev) target="the latest dev build" ;;
    esac
    cat >/dev/tty <<EOF

What do you want to do?
  1) Update to ${target}, keeping configuration and data
  2) Uninstall OpenCuria completely, including all workspaces and data

EOF
    ask choice "Choice" 1
    case "${choice}" in
      1) ACTION=update ;;
      2) ACTION=uninstall ;;
      *) die "Invalid choice: ${choice}" ;;
    esac
    return 0
  fi

  [[ "${TTY}" -eq 1 ]] || die "No terminal to ask on: pass an action (full, server or runner)."
  if has_leftovers; then
    warn "Found remains of an earlier installation (workspace VMs or runner data). To remove them first: curl -fsSL ${INSTALL_URL} | sudo bash -s -- uninstall"
  fi
  cat >/dev/tty <<'EOF'

What should this host run?
  1) Everything: control plane and a runner, wired together
  2) Control plane only: backend, web app, database and HTTPS proxy
  3) Runner only: connect this host to an existing control plane

EOF
  ask choice "Choice" 1
  case "${choice}" in
    1) ACTION=full ;;
    2) ACTION=server ;;
    3) ACTION=runner ;;
    *) die "Invalid choice: ${choice}" ;;
  esac
}

# -- questions -------------------------------------------------------------------

validate_server_env() {
  local env_file="${INSTALL_DIR}/.env" key
  for key in PUBLIC_URL DJANGO_SECRET_KEY DB_PASSWORD CREDENTIAL_ENCRYPTION_KEY; do
    [[ -n "$(env_value "${env_file}" "${key}")" ]] ||
      die "${env_file} has no ${key}. Restore the file from your backup, or uninstall OpenCuria to start over."
  done
}

collect_server_settings() {
  if [[ "${SERVER_ENV}" -eq 1 ]]; then
    validate_server_env
    [[ "${DOMAIN_SET}" -eq 0 ]] ||
      warn "--domain is ignored for an existing control plane; PUBLIC_URL in ${INSTALL_DIR}/.env stays."
    # The configuration survived but the database did not: bootstrap again.
    if [[ "${SERVER_READY}" -eq 1 && "${DOCKER_STATE}" != down && "${CP_DATA}" -eq 0 ]]; then
      SERVER_READY=0
      DATABASE_RESET=1
      [[ -n "${ADMIN_EMAIL}" ]] ||
        ADMIN_EMAIL="$(head -n 1 "${INSTALL_DIR}/.bootstrapped" 2>/dev/null || true)"
    fi
  else
    if [[ "${CP_DATA}" -eq 1 ]]; then
      die "Found the control plane database (Docker volume ${DB_VOLUME}) but not ${INSTALL_DIR}/.env, which holds its password and encryption key. Restore .env from your backup, or uninstall OpenCuria to start over."
    fi
    if [[ "${DOMAIN_SET}" -eq 0 ]]; then
      ask DOMAIN "Domain pointing to this host (empty: plain HTTP on $(detect_ip))"
    fi
    DOMAIN="${DOMAIN#http://}"
    DOMAIN="${DOMAIN#https://}"
    DOMAIN="${DOMAIN%/}"
    [[ -n "${DOMAIN}" ]] || DOMAIN="$(detect_ip)"
    [[ "${DOMAIN}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
      die "Invalid domain: ${DOMAIN}"
  fi

  if [[ "${SERVER_READY}" -eq 0 ]]; then
    if [[ -z "${ADMIN_EMAIL}" || "${DATABASE_RESET}" -eq 1 ]]; then
      ask ADMIN_EMAIL "Admin email" "${ADMIN_EMAIL}"
    fi
    [[ "${ADMIN_EMAIL}" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] ||
      die "A valid admin email is required (--admin-email)."
    if [[ -z "${ADMIN_PASSWORD}" ]]; then
      ask_secret ADMIN_PASSWORD "Admin password (empty: generate one)"
    fi
    if [[ -z "${ADMIN_PASSWORD}" ]]; then
      ADMIN_PASSWORD="$(random_string 20)"
      PASSWORD_GENERATED=1
    fi
    [[ "${#ADMIN_PASSWORD}" -ge 8 ]] || die "The admin password needs at least 8 characters."
  fi
}

collect_runner_settings() {
  local env_file="${INSTALL_DIR}/runner/.env" current_url current_token keep=0
  current_url="$(env_value "${env_file}" RUNNER_BACKEND_URL)"
  current_token="$(env_value "${env_file}" RUNNER_API_TOKEN)"
  # Updates keep the configuration; so do runs without a terminal.
  if [[ "${ACTION}" == update || "${TTY}" -eq 0 ]]; then keep=1; fi

  if [[ "${ACTION}" == full ]]; then
    BACKEND_URL="${LOCAL_BACKEND_URL}"
  elif [[ -z "${BACKEND_URL}" ]]; then
    if [[ -n "${current_url}" && "${keep}" -eq 1 ]]; then
      BACKEND_URL="${current_url}"
    elif [[ "${ACTION}" == update && "${MODE}" == full ]]; then
      BACKEND_URL="${LOCAL_BACKEND_URL}"
    else
      ask BACKEND_URL "Control plane URL (e.g. https://opencuria.example.com)" "${current_url}"
    fi
  fi
  BACKEND_URL="${BACKEND_URL%/}"
  [[ "${BACKEND_URL}" =~ ^(https?|wss?)://[A-Za-z0-9.:_-]+$ ]] ||
    die "Invalid control plane URL: '${BACKEND_URL}' (use https://host[:port], --url)."

  if [[ -z "${RUNNER_TOKEN}" ]]; then
    # The local runner of a full installation keeps its token.
    if [[ -n "${current_token}" && ( "${keep}" -eq 1 || "${ACTION}" == full ) ]]; then
      RUNNER_TOKEN="${current_token}"
    elif [[ -n "${current_token}" ]]; then
      ask_secret RUNNER_TOKEN "Runner token (empty: keep the current one)"
      RUNNER_TOKEN="${RUNNER_TOKEN:-${current_token}}"
    elif [[ "${MODE}" == full && "${BACKEND_URL}" == "${LOCAL_BACKEND_URL}" &&
      "${SERVER_READY}" -eq 0 ]]; then
      RUNNER_TOKEN="$(runner_token)" # registered by the control plane bootstrap
    else
      info "Create a runner token in the web app: Settings > Runners > Register Runner."
      ask_secret RUNNER_TOKEN "Runner token"
      [[ -n "${RUNNER_TOKEN}" ]] ||
        die "This runner has no token: create one under Settings > Runners > Register Runner and pass it with --token."
    fi
  fi
  [[ "${RUNNER_TOKEN}" =~ ^[A-Za-z0-9_-]{16,}$ ]] || die "This does not look like a runner token."
}

check_runner_host() {
  [[ "$(uname -m)" == "x86_64" ]] || die "The runner needs an x86_64 host."
  [[ -e /dev/kvm ]] ||
    die "/dev/kvm is missing. Enable hardware (or nested) virtualization for this host."
  command -v apt-get >/dev/null || die "The runner installer supports Debian and Ubuntu (apt)."
  has_systemd || die "The runner needs systemd."
}

check_ports_free() {
  # Our own proxy may hold the ports of an earlier, unfinished installation.
  [[ "${SERVER_ENV}" -eq 0 && "${#CP_CONTAINERS[@]}" -eq 0 ]] || return 0
  command -v ss >/dev/null || return 0
  local port
  for port in 80 443; do
    if ss -Hltn "( sport = :${port} )" | grep -q .; then
      die "Port ${port} is already in use. Free it, or run the control plane behind your own reverse proxy (README)."
    fi
  done
}

check_backend_reachable() {
  local url="${BACKEND_URL/#ws/http}/api/v1/health/" # ws(s):// speaks http(s) too
  curl -fsS --max-time 15 -o /dev/null "${url}" ||
    die "Cannot reach the control plane at ${url} from this host."
}

public_url_for() {
  if [[ "$1" =~ ^[0-9.]+$ || "$1" != *.* ]]; then
    printf 'http://%s' "$1"
  else
    printf 'https://%s' "$1"
  fi
}

summarize_plan() {
  step "Plan"
  if [[ "${ACTION}" == update ]]; then
    info "Update to ${VERSION}; configuration and data are kept"
  fi
  if [[ "${MODE}" != runner ]]; then
    if [[ "${SERVER_ENV}" -eq 1 ]]; then
      info "Control plane in ${INSTALL_DIR}: images ${VERSION}, $(env_value "${INSTALL_DIR}/.env" PUBLIC_URL)"
    else
      info "Control plane in ${INSTALL_DIR}: $(public_url_for "${DOMAIN}")"
    fi
    [[ "${SERVER_READY}" -eq 1 ]] || info "Admin: ${ADMIN_EMAIL}"
  fi
  if [[ "${MODE}" != server ]]; then
    info "Runner in ${INSTALL_DIR}/runner: connects to ${BACKEND_URL}"
  fi
  if [[ "${DATABASE_RESET}" -eq 1 ]]; then
    warn "The database volume ${DB_VOLUME} is missing: a new, empty database is created."
  fi
  if [[ "${INSTALLED_VERSION}" == dev && "${VERSION}" != dev ]]; then
    warn "Leaving dev for ${VERSION}: the database keeps the migrations dev applied. Downgrades are not supported; if ${VERSION} lacks them, the control plane may not start."
  fi
  if [[ "${MODE}" != server && "${RUNNER_FILES}" -eq 1 ]]; then
    warn "Updating restarts the runner; running agent commands are interrupted."
  fi
  confirm "Continue?" || die "Aborted; nothing was changed."
}

# -- source ----------------------------------------------------------------------

fetch_source() {
  local ref="${VERSION}"
  [[ "${ref}" != "latest" ]] || ref="main"
  local url="${OPENCURIA_SOURCE_URL:-https://github.com/${REPO}/archive/${ref}.tar.gz}"
  WORK_DIR="$(mktemp -d)"
  info "Downloading ${url}"
  curl -fsSL "${url}" | tar -xz -C "${WORK_DIR}" --strip-components=1 ||
    die "Could not download OpenCuria ${ref} from ${url}."
  [[ -f "${WORK_DIR}/compose.yml" && -d "${WORK_DIR}/runner" ]] ||
    die "The download from ${url} is not an OpenCuria source archive."
  SRC_DIR="${WORK_DIR}"
}

# The installer of the version being installed does the work, so installer,
# compose.yml and runner always come from one commit. It reuses this download
# (OPENCURIA_INSTALLER_SOURCE) and this shell removes it afterwards.
run_versioned_installer() {
  local args=() status=0
  step "Fetching OpenCuria ${VERSION}"
  fetch_source
  [[ -f "${SRC_DIR}/install.sh" ]] || return 0
  # Older installers know --version but not --dev.
  args=("${ACTION}" --dir "${INSTALL_DIR}" --version "${VERSION}")
  if [[ "${DOMAIN_SET}" -eq 1 ]]; then args+=(--domain "${DOMAIN}"); fi
  if [[ -n "${ADMIN_EMAIL}" ]]; then args+=(--admin-email "${ADMIN_EMAIL}"); fi
  if [[ -n "${ADMIN_PASSWORD}" ]]; then args+=(--admin-password "${ADMIN_PASSWORD}"); fi
  if [[ -n "${BACKEND_URL}" ]]; then args+=(--url "${BACKEND_URL}"); fi
  if [[ -n "${RUNNER_TOKEN}" ]]; then args+=(--token "${RUNNER_TOKEN}"); fi
  if [[ "${ASSUME_YES}" -eq 1 ]]; then args+=(--yes); fi
  # Ctrl-C reaches both shells; the versioned installer reports it.
  trap : INT TERM
  OPENCURIA_INSTALLER_SOURCE="${SRC_DIR}" bash "${SRC_DIR}/install.sh" "${args[@]}" ||
    status=$?
  exit "${status}"
}

use_installer_source() {
  local dir="${OPENCURIA_INSTALLER_SOURCE:-}"
  [[ -n "${dir}" ]] || return 0
  [[ -f "${dir}/compose.yml" && -d "${dir}/runner" ]] ||
    die "OPENCURIA_INSTALLER_SOURCE=${dir} is not an OpenCuria source tree."
  SRC_DIR="${dir}"
}

# -- control plane ---------------------------------------------------------------

ensure_docker() {
  if ! command -v docker >/dev/null; then
    confirm "Docker is not installed. Install it with the official script from get.docker.com?" ||
      die "Docker is required for the control plane."
    step "Installing Docker"
    curl -fsSL https://get.docker.com | sh || die "Installing Docker failed (see the output above)."
    command -v docker >/dev/null || die "Docker is still not installed."
  fi
  docker compose version >/dev/null 2>&1 ||
    die "Docker is installed without the Compose plugin. Install docker-compose-plugin and re-run."
  start_docker 1
}

# The mobile app's end-to-end encrypted push proxy; empty turns push off.
PUSH_PROXY_ENV="PUSH_PROXY_URL=https://push.opencuria.dev"

write_server_env() {
  local env_file="${INSTALL_DIR}/.env"
  install -m 600 /dev/null "${env_file}"
  cat >"${env_file}" <<EOF
# Written by install.sh; compose.yml shows how each value is used. Keep this file
# secret and backed up: it holds the database password and the encryption key.
OPENCURIA_TAG=${VERSION}
COMPOSE_PROFILES=proxy
PUBLIC_URL=$(public_url_for "${DOMAIN}")
DJANGO_ALLOWED_HOSTS=${DOMAIN},127.0.0.1
DJANGO_SECRET_KEY=$(random_string 64)
DB_PASSWORD=$(random_string 32)
CREDENTIAL_ENCRYPTION_KEY=$(fernet_key)
${PUSH_PROXY_ENV}
EOF
}

install_control_plane() {
  step "Setting up the control plane in ${INSTALL_DIR}"
  install -d -m 755 "${INSTALL_DIR}"
  install -m 644 "${SRC_DIR}/compose.yml" "${INSTALL_DIR}/compose.yml"
  install -m 644 "${SRC_DIR}/Caddyfile" "${INSTALL_DIR}/Caddyfile"
  if [[ "${SERVER_ENV}" -eq 0 ]]; then
    write_server_env
  else
    set_env_value "${INSTALL_DIR}/.env" OPENCURIA_TAG "${VERSION}"
    # Older installs predate the push proxy; keep an explicit (even empty) value.
    grep -q '^PUSH_PROXY_URL=' "${INSTALL_DIR}/.env" ||
      printf '%s\n' "${PUSH_PROXY_ENV}" >>"${INSTALL_DIR}/.env"
  fi
  # Only a finished bootstrap marks the control plane as ready.
  [[ "${SERVER_READY}" -eq 1 ]] || rm -f "${INSTALL_DIR}/.bootstrapped"

  step "Pulling images"
  compose pull ||
    die "Could not pull the OpenCuria images (${VERSION}). Check access to ghcr.io and the version."
  step "Starting services"
  compose up -d --remove-orphans --wait --wait-timeout 600 ||
    die "Services did not become healthy. Inspect: cd ${INSTALL_DIR} && docker compose ps && docker compose logs"

  if [[ "${SERVER_READY}" -eq 0 ]]; then
    bootstrap_control_plane
  fi
  PUBLIC_URL="$(env_value "${INSTALL_DIR}/.env" PUBLIC_URL)"
}

bootstrap_control_plane() {
  step "Creating the admin account"
  local args=(--admin-email "${ADMIN_EMAIL}")
  if [[ "${MODE}" != server && "${BACKEND_URL}" == "${LOCAL_BACKEND_URL}" ]]; then
    args+=(--runner-name "$(hostname -s)")
  else
    args+=(--no-runner)
  fi
  # Secrets travel as environment variables, never as visible arguments.
  OPENCURIA_ADMIN_PASSWORD="${ADMIN_PASSWORD}" OPENCURIA_RUNNER_TOKEN="${RUNNER_TOKEN}" \
    compose exec -T -e OPENCURIA_ADMIN_PASSWORD -e OPENCURIA_RUNNER_TOKEN backend \
    python manage.py bootstrap_deployment "${args[@]}" ||
    die "Creating the admin account failed; re-run the installer to retry."

  printf '{"email":"%s","password":"%s"}' "$(json_escape "${ADMIN_EMAIL}")" \
    "$(json_escape "${ADMIN_PASSWORD}")" |
    curl -fsS -o /dev/null -H 'Content-Type: application/json' --data @- \
      "${LOCAL_BACKEND_URL}/api/v1/auth/login/" ||
    die "The admin account was created but logging in with it failed."
  printf '%s\n' "${ADMIN_EMAIL}" >"${INSTALL_DIR}/.bootstrapped"
}

# -- runner ----------------------------------------------------------------------

# Docker sets the iptables FORWARD policy to DROP. libvirt's nftables backend
# cannot override that verdict, which cuts workspace VMs off the network.
pin_libvirt_firewall_backend() {
  local conf="${LIBVIRT_NETWORK_CONF}"
  [[ -f "${conf}" ]] && grep -q firewall_backend "${conf}" || return 1
  grep -Eq '^[[:space:]]*firewall_backend[[:space:]]*=' "${conf}" && return 1
  printf '\n%s\nfirewall_backend = "iptables"\n' "${LIBVIRT_PIN_COMMENT}" >>"${conf}"
}

# Remove the pin (and the blank line before it) that pin_libvirt_firewall_backend added.
unpin_libvirt_firewall_backend() {
  local conf="${LIBVIRT_NETWORK_CONF}" tmp
  [[ -f "${conf}" ]] && grep -qxF "${LIBVIRT_PIN_COMMENT}" "${conf}" || return 0
  tmp="$(mktemp)"
  awk -v mark="${LIBVIRT_PIN_COMMENT}" '
    skip && /^firewall_backend = "iptables"$/ { skip = 0; next }
    { skip = 0 }
    $0 == mark { held = ""; skip = 1; next }
    /^$/ { held = held $0 "\n"; next }
    { printf "%s", held; held = ""; print }
    END { printf "%s", held }
  ' "${conf}" >"${tmp}"
  cat "${tmp}" >"${conf}"
  rm -f "${tmp}"
}

apt_get() {
  # needrestart would otherwise restart services such as Docker mid-install.
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
    apt-get -o DPkg::Lock::Timeout=300 "$@"
}

install_runner() {
  local dir="${INSTALL_DIR}/runner" new="${INSTALL_DIR}/runner.new"
  local old="${INSTALL_DIR}/runner.old" python_ok

  step "Installing QEMU/KVM and build dependencies"
  apt_get update -qq || die "apt-get update failed (see the output above)."
  apt_get install -y -qq \
    qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients genisoimage \
    ovmf swtpm-tools iptables openssh-client build-essential pkg-config \
    libvirt-dev python3-dev python3-venv >/dev/null ||
    die "Installing the QEMU/KVM packages failed (see the output above)."
  python_ok="$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')"
  [[ "${python_ok}" == "1" ]] || die "The runner needs Python 3.10 or newer."

  if pin_libvirt_firewall_backend; then
    systemctl restart libvirtd || die "Restarting libvirtd failed. Check: systemctl status libvirtd"
  fi
  systemctl enable --now libvirtd >/dev/null 2>&1 ||
    die "Could not start libvirtd. Check: systemctl status libvirtd"
  # Workspace storage holds tenant data. QEMU only needs to traverse it; the
  # runner keeps the files owner-only and tightens older ones on start.
  install -d -m 711 "${DATA_DIR}" "${DATA_DIR}"/{images,base-images,disks,snapshots}

  step "Installing the runner in ${dir}"
  rm -rf "${new}" "${old}"
  install -d -m 755 "${INSTALL_DIR}"
  cp -a "${SRC_DIR}/runner" "${new}"
  rm -rf "${new}/.venv" "${new}/.env"
  if [[ -d "${dir}/.venv" ]]; then mv "${dir}/.venv" "${new}/.venv"; fi
  if [[ -f "${dir}/.env" ]]; then
    cp -p "${dir}/.env" "${new}/.env"
  else
    install -m 600 "${new}/.env.example" "${new}/.env"
  fi
  # .env in runner.new marks it complete for recover_runner_swap.
  if [[ -d "${dir}" ]]; then mv "${dir}" "${old}"; fi
  mv "${new}" "${dir}"
  rm -rf "${old}"

  if [[ ! -x "${dir}/.venv/bin/python" ]] || ! "${dir}/.venv/bin/python" -c '' 2>/dev/null; then
    rm -rf "${dir}/.venv"
    python3 -m venv "${dir}/.venv" || die "Creating the runner's Python environment failed."
  fi
  if ! "${dir}/.venv/bin/python" -m pip install --quiet --upgrade pip ||
    ! "${dir}/.venv/bin/python" -m pip install --quiet -r "${dir}/requirements.txt"; then
    die "Installing the runner's Python packages failed (see the output above)."
  fi

  chmod 600 "${dir}/.env"
  set_env_value "${dir}/.env" RUNNER_BACKEND_URL "${BACKEND_URL}"
  set_env_value "${dir}/.env" RUNNER_API_TOKEN "${RUNNER_TOKEN}"
  set_env_value "${dir}/.env" OPENCURIA_TAG "${VERSION}" # the installer's, not the runner's

  # Replacing the file also clears a mask (a symlink to /dev/null).
  rm -f "${RUNNER_UNIT}"
  sed "s|__RUNNER_DIR__|${dir}|g" "${dir}/systemd/${RUNNER_SERVICE}.service" >"${RUNNER_UNIT}"
  systemctl daemon-reload
  systemctl enable --quiet "${RUNNER_SERVICE}" ||
    die "Could not enable ${RUNNER_SERVICE}. Check: systemctl status ${RUNNER_SERVICE}"

  step "Starting the runner"
  local since
  since="$(date +%s)"
  systemctl reset-failed "${RUNNER_SERVICE}" >/dev/null 2>&1 || true
  systemctl restart "${RUNNER_SERVICE}" ||
    die "Could not start ${RUNNER_SERVICE}. Check: journalctl -u ${RUNNER_SERVICE} -n 50"
  wait_for_runner "${since}"
}

wait_for_runner() {
  local deadline=$((SECONDS + 120)) logs
  while ((SECONDS < deadline)); do
    logs="$(journalctl -u "${RUNNER_SERVICE}" --since "@$1" -o cat --no-pager 2>/dev/null)" ||
      logs=""
    if grep -q websocket_connected <<<"${logs}"; then
      info "Runner connected to ${BACKEND_URL}"
      return 0
    fi
    if grep -q websocket_connect_error <<<"${logs}"; then
      grep websocket_connect_error <<<"${logs}" | tail -n 1 >&2
      die "The control plane rejected the runner token. Create a new one under Settings > Runners > Register Runner and re-run with --token TOKEN."
    fi
    sleep 3
  done
  journalctl -u "${RUNNER_SERVICE}" --since "@$1" -n 20 --no-pager >&2 || true
  die "The runner did not connect within 2 minutes. Logs: journalctl -u ${RUNNER_SERVICE} -f"
}

# -- uninstall -------------------------------------------------------------------

# Storage that runner/.env moved out of the default directories.
custom_runner_paths() {
  local env_file="${INSTALL_DIR}/runner/.env" key path
  for key in "${RUNNER_PATH_KEYS[@]}"; do
    path="$(env_value "${env_file}" "${key}")"
    [[ -n "${path}" && "${path}" != "${DATA_DIR}" && "${path}" != "${DATA_DIR}"/* &&
      "${path}" != "${STATE_DIR}" && "${path}" != "${ROOT_SHARE_DIR}"/* ]] || continue
    printf '%s\n' "${path}"
  done
}

# Only paths that clearly belong to OpenCuria are deleted.
removable_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ && "$1" != *..* && "${1,,}" == *opencuria* ]]
}

uninstall_plan() {
  local paths=() path entry
  step "Plan: uninstall OpenCuria"
  if runner_installed; then info "Runner service ${RUNNER_SERVICE}"; fi
  if [[ "${#VM_DOMAINS[@]}" -gt 0 || "${#VM_NETWORKS[@]}" -gt 0 ]]; then
    info "Workspaces: $(plural "${#VM_DOMAINS[@]}" VM) (${VM_RUNNING} running) and $(plural "${#VM_NETWORKS[@]}" network)"
  fi
  if [[ "${#CP_CONTAINERS[@]}" -gt 0 || "${#CP_VOLUMES[@]}" -gt 0 ]]; then
    info "Control plane: $(plural "${#CP_CONTAINERS[@]}" container), $(plural "${#CP_VOLUMES[@]}" volume) (database, files, certificates)"
  fi
  if [[ "${#CP_IMAGES[@]}" -gt 0 ]]; then
    info "Docker images: $(plural "${#CP_IMAGES[@]}" image) of ghcr.io/opencuria"
  fi
  for entry in "${INSTALL_ENTRIES[@]}"; do
    if [[ -e "${INSTALL_DIR}/${entry}" ]]; then
      paths+=("${INSTALL_DIR}")
      break
    fi
  done
  paths+=("${LEFTOVER_PATHS[@]}")
  while IFS= read -r path; do
    [[ -n "${path}" && -e "${path}" ]] || continue
    if removable_path "${path}"; then paths+=("${path}"); fi
  done < <(custom_runner_paths)
  if [[ -e "${RUNNER_UNIT}" || -L "${RUNNER_UNIT}" ]]; then paths+=("${RUNNER_UNIT}"); fi
  [[ "${#paths[@]}" -eq 0 ]] || info "Files: ${paths[*]}"
  if [[ "${DOCKER_STATE}" == down ]]; then
    info "Not checked: Docker containers (Docker is not running)"
  fi
  if [[ "${LIBVIRT_STATE}" == down ]]; then
    info "Not checked: workspace VMs (libvirt is not running)"
  fi
  info "Kept: system packages such as Docker, QEMU and libvirt"
  warn "This permanently deletes all workspaces, chats, users and settings on this host."
}

confirm_uninstall() {
  local answer=""
  [[ "${ASSUME_YES}" -eq 0 ]] || return 0
  [[ "${TTY}" -eq 1 ]] ||
    die "Uninstalling deletes all data; without a terminal, confirm with --yes."
  ask answer 'Type "uninstall" to delete everything'
  [[ "${answer}" == uninstall ]] || die "Aborted; nothing was changed."
}

stop_runner() {
  step "Stopping the runner"
  if has_systemd; then
    systemctl stop "${RUNNER_SERVICE}.service" >/dev/null 2>&1 || true
    systemctl disable "${RUNNER_SERVICE}.service" >/dev/null 2>&1 || true
    if systemctl is-active --quiet "${RUNNER_SERVICE}.service"; then
      die "Could not stop ${RUNNER_SERVICE}. Check: systemctl status ${RUNNER_SERVICE}"
    fi
  fi
  # Runners started by hand from the installation directory.
  local pattern="${INSTALL_DIR}/runner/\\.venv/bin/python" deadline=$((SECONDS + 20))
  pgrep -f "${pattern}" >/dev/null || return 0
  pkill -TERM -f "${pattern}" || true
  while pgrep -f "${pattern}" >/dev/null; do
    if ((SECONDS >= deadline)); then
      pkill -KILL -f "${pattern}" || true
      sleep 1
      if pgrep -f "${pattern}" >/dev/null; then
        die "Could not stop the runner processes: $(pgrep -af "${pattern}" | head -n 3)"
      fi
      break
    fi
    sleep 1
  done
}

remove_control_plane() {
  [[ "${DOCKER_STATE}" == up ]] || return 0
  [[ "${#CP_CONTAINERS[@]}" -gt 0 || "${#CP_VOLUMES[@]}" -gt 0 ||
    "${#CP_NETWORKS[@]}" -gt 0 || "${#CP_IMAGES[@]}" -gt 0 ]] || return 0
  step "Removing the control plane"
  local out images=() image
  if [[ "${#CP_CONTAINERS[@]}" -gt 0 ]]; then
    out="$(docker container inspect --format '{{.Config.Image}}' "${CP_CONTAINERS[@]}" 2>/dev/null)" ||
      out=""
    lines_to images "${out}"
    out="$(docker container rm -f "${CP_CONTAINERS[@]}" 2>&1)" ||
      die "Could not remove the control plane containers: ${out}"
  fi
  if [[ "${#CP_VOLUMES[@]}" -gt 0 ]]; then
    out="$(docker volume rm -f "${CP_VOLUMES[@]}" 2>&1)" ||
      die "Could not remove the control plane volumes: ${out}"
  fi
  if [[ "${#CP_NETWORKS[@]}" -gt 0 ]]; then
    out="$(docker network rm "${CP_NETWORKS[@]}" 2>&1)" ||
      die "Could not remove the control plane network: ${out}"
  fi
  # OpenCuria images in every tag; third-party images unless something else uses them.
  if [[ "${#CP_IMAGES[@]}" -gt 0 ]]; then
    out="$(docker image rm -f "${CP_IMAGES[@]}" 2>&1)" ||
      warn "Kept OpenCuria images that other containers use: ${out}"
  fi
  for image in "${images[@]}"; do
    [[ "${image}" != ghcr.io/opencuria/* ]] || continue
    docker image rm "${image}" >/dev/null 2>&1 || info "Kept image ${image} (in use)"
  done

  detect_docker
  [[ "${DOCKER_STATE}" == up ]] || die "Docker stopped responding while removing the control plane."
  [[ "${#CP_CONTAINERS[@]}" -eq 0 && "${#CP_VOLUMES[@]}" -eq 0 ]] ||
    die "Still present after removal: ${CP_CONTAINERS[*]-} ${CP_VOLUMES[*]-}"
  info "Removed"
}

remove_domain() {
  local name="$1" state out
  state="$(virsh_ domstate "${name}" 2>/dev/null)" || return 0 # already gone
  state="${state%%$'\n'*}"
  if [[ "${state}" != "shut off" ]]; then
    out="$(virsh_ destroy "${name}" 2>&1)" || {
      state="$(virsh_ domstate "${name}" 2>/dev/null)" || return 0
      [[ "${state%%$'\n'*}" == "shut off" ]] ||
        die "Could not stop the workspace VM ${name}: ${out}"
    }
  fi
  # A transient domain is gone once it is stopped.
  virsh_ dominfo "${name}" >/dev/null 2>&1 || return 0
  out="$(virsh_ undefine "${name}" --managed-save --snapshots-metadata \
    --checkpoints-metadata --nvram 2>&1)" ||
    out="$(virsh_ undefine "${name}" --nvram 2>&1)" ||
    out="$(virsh_ undefine "${name}" 2>&1)" ||
    die "Could not remove the workspace VM ${name}: ${out}"
}

remove_network() {
  local name="$1" out
  virsh_ net-info "${name}" >/dev/null 2>&1 || return 0
  virsh_ net-destroy "${name}" >/dev/null 2>&1 || true # fails when already inactive
  virsh_ net-info "${name}" >/dev/null 2>&1 || return 0 # transient: gone
  out="$(virsh_ net-undefine "${name}" 2>&1)" ||
    die "Could not remove the workspace network ${name}: ${out}"
}

remove_workspaces() {
  [[ "${LIBVIRT_STATE}" == up ]] || return 0
  [[ "${#VM_DOMAINS[@]}" -gt 0 || "${#VM_NETWORKS[@]}" -gt 0 ]] || return 0
  step "Removing workspace VMs"
  local name
  for name in "${VM_DOMAINS[@]}"; do remove_domain "${name}"; done
  for name in "${VM_NETWORKS[@]}"; do remove_network "${name}"; done
  detect_libvirt
  [[ "${LIBVIRT_STATE}" == up ]] || die "libvirt stopped responding while removing workspace VMs."
  [[ "${#VM_DOMAINS[@]}" -eq 0 && "${#VM_NETWORKS[@]}" -eq 0 ]] ||
    die "Still present after removal: ${VM_DOMAINS[*]-} ${VM_NETWORKS[*]-}"
  info "Removed"
}

remove_files() {
  step "Removing files"
  local path entry kept
  # Custom storage first: the paths come from runner/.env.
  while IFS= read -r path; do
    [[ -n "${path}" && ( -e "${path}" || -L "${path}" ) ]] || continue
    if removable_path "${path}"; then
      rm -rf -- "${path}"
      [[ ! -f "${path}.pub" ]] || rm -f -- "${path}.pub"
    else
      warn "Kept ${path} (set in runner/.env); delete it yourself if it only holds OpenCuria data."
    fi
  done < <(custom_runner_paths)
  for path in "${LEFTOVER_PATHS[@]}"; do rm -rf -- "${path}"; done

  if [[ -e "${RUNNER_UNIT}" || -L "${RUNNER_UNIT}" || -d "${RUNNER_UNIT}.d" ]]; then
    rm -rf -- "${RUNNER_UNIT}" "${RUNNER_UNIT}.d"
    if has_systemd; then
      systemctl daemon-reload || warn "systemctl daemon-reload failed."
      systemctl reset-failed "${RUNNER_SERVICE}.service" >/dev/null 2>&1 || true
    fi
  fi

  for entry in "${INSTALL_ENTRIES[@]}"; do rm -rf -- "${INSTALL_DIR:?}/${entry}"; done
  if [[ -d "${INSTALL_DIR}" ]] && ! rmdir "${INSTALL_DIR}" 2>/dev/null; then
    kept="$(find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 -printf '%f ' | head -c 200)"
    info "Kept ${INSTALL_DIR}: it holds other files (${kept% })"
  fi

  # The pin only keeps libvirt working next to Docker.
  if ! command -v docker >/dev/null; then unpin_libvirt_firewall_backend; fi
  info "Removed"
}

verify_uninstalled() {
  local left=()
  detect_files
  [[ "${DOCKER_STATE}" != up ]] || detect_docker
  [[ "${LIBVIRT_STATE}" != up ]] || detect_libvirt
  if server_installed; then left+=("control plane"); fi
  if runner_installed; then left+=("runner"); fi
  if has_leftovers; then left+=("workspace VMs or runner data"); fi
  [[ "${#left[@]}" -eq 0 ]] ||
    die "Uninstall incomplete; still present: ${left[*]}. Run the uninstaller again."
}

uninstall() {
  # Docker and libvirt must run to see and remove containers and VMs. Start
  # them only where traces of OpenCuria point to something to remove.
  if [[ "${DOCKER_STATE}" == down ]] && server_installed; then
    start_docker 0
    detect_docker
  fi
  if [[ "${LIBVIRT_STATE}" == down ]] && { runner_installed || has_leftovers; }; then
    start_libvirt
    detect_libvirt
  fi
  if ! server_installed && ! runner_installed && ! has_leftovers &&
    [[ "${#CP_IMAGES[@]}" -eq 0 ]]; then
    info "OpenCuria is not installed on this host (${INSTALL_DIR}); nothing to remove."
    return 0
  fi

  uninstall_plan
  confirm_uninstall
  local had_server=0
  if server_installed; then had_server=1; fi
  stop_runner
  remove_control_plane
  remove_workspaces
  remove_files
  verify_uninstalled

  printf '\n%sOpenCuria has been removed from this host.%s\n' "${C_GREEN}${C_BOLD}" "${C_RESET}"
  if [[ "${had_server}" -eq 0 ]]; then
    printf '  - The control plane still lists this runner: delete it under Settings > Runners.\n'
  fi
  printf '  - Docker, QEMU and libvirt stay installed; remove them with apt if unused.\n'
}

# -- install and update ----------------------------------------------------------

install_or_update() {
  case "${ACTION}" in
    full | server | runner) MODE="${ACTION}" ;;
    update)
      if server_installed && runner_installed; then
        MODE=full
      elif server_installed; then
        MODE=server
      elif runner_installed; then
        MODE=runner
      else
        die "OpenCuria is not installed on this host (${INSTALL_DIR}); nothing to update. Install it with: curl -fsSL ${INSTALL_URL} | sudo bash"
      fi
      ;;
  esac

  if [[ "${MODE}" != server ]]; then check_runner_host; fi
  if [[ "${MODE}" != runner ]]; then
    # The database volume is only visible while Docker runs.
    if [[ "${DOCKER_STATE}" == down ]]; then
      start_docker 1
      detect_docker
    fi
    collect_server_settings
    check_ports_free
  fi
  if [[ "${MODE}" != server ]]; then collect_runner_settings; fi
  if [[ "${MODE}" == runner ]]; then check_backend_reachable; fi
  summarize_plan

  if [[ -z "${SRC_DIR}" ]]; then
    step "Fetching OpenCuria ${VERSION}"
    fetch_source
  fi

  if [[ "${MODE}" != runner ]]; then
    ensure_docker
    install_control_plane
  fi
  if [[ "${MODE}" != server ]]; then install_runner; fi
  print_summary
}

print_summary() {
  if [[ "${ACTION}" == update ]]; then
    printf '\n%sOpenCuria is up to date (%s).%s\n\n' "${C_GREEN}${C_BOLD}" "${VERSION}" "${C_RESET}"
  else
    printf '\n%sOpenCuria is ready.%s\n\n' "${C_GREEN}${C_BOLD}" "${C_RESET}"
  fi
  if [[ "${MODE}" != "runner" ]]; then
    printf '  Web app:   %s\n' "${PUBLIC_URL}"
    if [[ -n "${ADMIN_EMAIL}" ]]; then
      printf '  Admin:     %s\n' "${ADMIN_EMAIL}"
    fi
    if [[ "${PASSWORD_GENERATED}" -eq 1 ]]; then
      printf '  Password:  %s  (generated; store it now, it is not saved)\n' "${ADMIN_PASSWORD}"
    fi
    printf '  Files:     %s (docker compose ps|logs there)\n' "${INSTALL_DIR}"
  fi
  if [[ "${MODE}" != "server" ]]; then
    printf '  Runner:    systemctl status %s; journalctl -u %s -f\n' \
      "${RUNNER_SERVICE}" "${RUNNER_SERVICE}"
  fi
  printf '\nNext steps:\n'
  if [[ "${MODE}" == "server" && "${ACTION}" != update ]]; then
    printf '  - Add a runner: Settings > Runners > Register Runner shows the command\n'
    printf '    to run on a Linux host with KVM.\n'
  fi
  if [[ "${MODE}" != "runner" && "${PUBLIC_URL}" == https://* ]]; then
    printf '  - HTTPS needs DNS for %s pointing here and ports 80/443 reachable.\n' \
      "${PUBLIC_URL#https://}"
  fi
  if [[ "${ACTION}" != update ]]; then
    printf '  - Build an image under Settings > Image Definitions and connect a model\n'
    printf '    provider under Settings > Provider & Models; the home screen guides you.\n'
  fi
  printf '  - Update or uninstall: run the install command again.\n'
}

main() {
  parse_args "$@"
  trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR
  trap on_interrupt INT TERM
  trap cleanup EXIT
  [[ "${EUID}" -eq 0 ]] || die "Run as root: curl -fsSL ${INSTALL_URL} | sudo bash"
  if [[ "${ASSUME_YES}" -eq 0 ]] && (exec </dev/tty) 2>/dev/null; then
    TTY=1
  fi
  ROOT_SHARE_DIR="$(getent passwd root | cut -d: -f6)" || true
  ROOT_SHARE_DIR="${ROOT_SHARE_DIR:-/root}/.local/share/opencuria"
  use_installer_source
  if [[ -z "${SRC_DIR}" ]]; then
    printf '%sOpenCuria installer%s\n' "${C_BOLD}" "${C_RESET}" >&2
  fi

  CURRENT_STEP="Detecting the installation"
  resolve_install_dir
  recover_runner_swap
  local project
  project="$(env_value "${INSTALL_DIR}/.env" COMPOSE_PROJECT_NAME)"
  if [[ "${project}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    COMPOSE_PROJECT="${project}"
    COMPOSE_LABEL="label=com.docker.compose.project=${COMPOSE_PROJECT}"
    DB_VOLUME="${COMPOSE_PROJECT}_pgdata"
  fi
  detect_files
  detect_docker
  detect_libvirt
  resolve_version
  choose_action

  if [[ "${ACTION}" == uninstall ]]; then
    uninstall
  else
    [[ -n "${SRC_DIR}" ]] || run_versioned_installer
    install_or_update
  fi
}

main "$@"
