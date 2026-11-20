#!/usr/bin/env bash
# OpenCuria installer: control plane (Docker Compose), QEMU runner (systemd) or both.
#
#   curl -fsSL https://raw.githubusercontent.com/opencuria/OpenCuria/main/install.sh | sudo bash
#
# Re-running it updates an existing installation in place. See --help for
# non-interactive use. Everything runs from main() so a truncated download
# never executes half a script.

set -euo pipefail

REPO="opencuria/OpenCuria"
INSTALL_URL="https://raw.githubusercontent.com/${REPO}/main/install.sh"
RUNNER_SERVICE="opencuria-runner"
LOCAL_BACKEND_URL="http://127.0.0.1:8000"

MODE=""
DOMAIN=""
DOMAIN_SET=0
ADMIN_EMAIL=""
ADMIN_PASSWORD=""
PASSWORD_GENERATED=0
BACKEND_URL=""
RUNNER_TOKEN=""
INSTALL_DIR="/opt/opencuria"
VERSION="latest"
ASSUME_YES=0

TTY=0
SRC_DIR=""
WORK_DIR=""
SERVER_EXISTS=0
SERVER_READY=0
RUNNER_EXISTS=0
PUBLIC_URL=""

usage() {
  cat <<EOF
Install or update OpenCuria on this host.

Usage:
  curl -fsSL ${INSTALL_URL} | sudo bash
  curl -fsSL ${INSTALL_URL} | sudo bash -s -- [MODE] [OPTIONS]

Modes (asked interactively when omitted):
  full      Control plane and a runner on this host, wired together
  server    Control plane only: backend, web app, PostgreSQL, Redis, HTTPS proxy
  runner    Runner only, connected to an existing control plane

Options:
  --domain NAME          Public domain; HTTPS via Let's Encrypt (DNS must point
                         here, ports 80/443 open). An IP address or no domain
                         serves plain HTTP on port 80.
  --admin-email EMAIL    First admin account (new control planes)
  --admin-password PASS  Its password (default: generated and printed once)
  --url URL              Runner: control plane URL, e.g. https://opencuria.example.com
  --token TOKEN          Runner: token from Settings > Runners > Register Runner
  --dir PATH             Installation directory (default: /opt/opencuria)
  --version VERSION      Image tag / commit to install (default: latest)
  -y, --yes              Never prompt; fail when a required value is missing
  -h, --help             Show this help

Re-run the same command to update. Configuration and data are kept.
EOF
}

# -- output ----------------------------------------------------------------

if [[ -t 2 ]]; then
  C_BOLD=$'\033[1m' C_RED=$'\033[31m' C_YELLOW=$'\033[33m' C_GREEN=$'\033[32m'
  C_RESET=$'\033[0m'
else
  C_BOLD="" C_RED="" C_YELLOW="" C_GREEN="" C_RESET=""
fi

step() { printf '\n%s==> %s%s\n' "${C_BOLD}" "$*" "${C_RESET}" >&2; }
info() { printf '    %s\n' "$*" >&2; }
warn() { printf '%sWarning:%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die() {
  printf '%sError:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2
  exit 1
}

# -- prompts (read from the terminal: stdin is the script under curl | bash) --

# ask VAR PROMPT [DEFAULT]
ask() {
  local reply="" prompt="$2"
  [[ -n "${3:-}" ]] && prompt+=" [$3]"
  if [[ "${TTY}" -eq 1 ]]; then
    read -r -p "${prompt}: " reply </dev/tty || true
  fi
  printf -v "$1" '%s' "${reply:-${3:-}}"
}

# ask_secret VAR PROMPT
ask_secret() {
  local reply=""
  if [[ "${TTY}" -eq 1 ]]; then
    read -r -s -p "$2: " reply </dev/tty || true
    printf '\n' >/dev/tty
  fi
  printf -v "$1" '%s' "${reply}"
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

env_value() { # env_value FILE KEY
  [[ -f "$1" ]] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

json_escape() {
  local s="${1//\\/\\\\}"
  printf '%s' "${s//\"/\\\"}"
}

detect_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
  [[ -n "${ip}" ]] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "${ip:-127.0.0.1}"
}

compose() { (cd "${INSTALL_DIR}" && docker compose "$@"); }

cleanup() { [[ -z "${WORK_DIR}" ]] || rm -rf "${WORK_DIR}"; }

# -- arguments and environment -----------------------------------------------

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      full | server | runner) MODE="$1" ;;
      --domain) DOMAIN="${2?--domain needs a value}" DOMAIN_SET=1; shift ;;
      --admin-email) ADMIN_EMAIL="${2?--admin-email needs a value}"; shift ;;
      --admin-password) ADMIN_PASSWORD="${2?--admin-password needs a value}"; shift ;;
      --url) BACKEND_URL="${2?--url needs a value}"; shift ;;
      --token) RUNNER_TOKEN="${2?--token needs a value}"; shift ;;
      --dir) INSTALL_DIR="${2?--dir needs a value}"; shift ;;
      --version) VERSION="${2?--version needs a value}"; shift ;;
      -y | --yes) ASSUME_YES=1 ;;
      -h | --help) usage; exit 0 ;;
      *) die "Unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  [[ "${VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || die "Invalid --version: ${VERSION}"
  INSTALL_DIR="${INSTALL_DIR%/}"
  [[ "${INSTALL_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "Invalid --dir: ${INSTALL_DIR}"
}

detect_state() {
  [[ -f "${INSTALL_DIR}/.env" ]] && SERVER_EXISTS=1
  [[ -f "${INSTALL_DIR}/.bootstrapped" ]] && SERVER_READY=1
  [[ -f "${INSTALL_DIR}/runner/.env" ]] && RUNNER_EXISTS=1
  if [[ "${ASSUME_YES}" -eq 0 ]] && (exec </dev/tty) 2>/dev/null; then
    TTY=1
  fi
  return 0
}

choose_mode() {
  [[ -z "${MODE}" ]] || return 0
  [[ "${TTY}" -eq 1 ]] || die "No terminal to ask on: pass a mode (full, server or runner)."
  local default=1 choice
  if [[ "${RUNNER_EXISTS}" -eq 1 && "${SERVER_EXISTS}" -eq 0 ]]; then
    default=3
  elif [[ "${SERVER_EXISTS}" -eq 1 && "${RUNNER_EXISTS}" -eq 0 ]]; then
    default=2
  fi
  cat >/dev/tty <<'EOF'

What should this host run?
  1) Everything: control plane and a runner, wired together
  2) Control plane only: backend, web app, database and HTTPS proxy
  3) Runner only: connect this host to an existing control plane

EOF
  ask choice "Choice" "${default}"
  case "${choice}" in
    1) MODE=full ;;
    2) MODE=server ;;
    3) MODE=runner ;;
    *) die "Invalid choice: ${choice}" ;;
  esac
}

# -- questions -----------------------------------------------------------------

collect_server_settings() {
  if [[ "${SERVER_EXISTS}" -eq 0 ]]; then
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
    [[ -n "${ADMIN_EMAIL}" ]] || ask ADMIN_EMAIL "Admin email"
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
  local env_file="${INSTALL_DIR}/runner/.env" current_url
  current_url="$(env_value "${env_file}" RUNNER_BACKEND_URL)"

  if [[ "${MODE}" == "full" ]]; then
    BACKEND_URL="${LOCAL_BACKEND_URL}"
    [[ -n "${RUNNER_TOKEN}" ]] || RUNNER_TOKEN="$(env_value "${env_file}" RUNNER_API_TOKEN)"
    if [[ -z "${RUNNER_TOKEN}" && "${SERVER_READY}" -eq 0 ]]; then
      RUNNER_TOKEN="$(runner_token)" # registered by the control plane bootstrap
    fi
  elif [[ -z "${BACKEND_URL}" && ( "${RUNNER_EXISTS}" -eq 0 || "${TTY}" -eq 1 ) ]]; then
    ask BACKEND_URL "Control plane URL (e.g. https://opencuria.example.com)" "${current_url}"
  fi

  if [[ -z "${RUNNER_TOKEN}" ]]; then
    if [[ "${RUNNER_EXISTS}" -eq 1 ]]; then
      ask_secret RUNNER_TOKEN "Runner token (empty: keep the current one)"
    else
      info "Create a token in the web app: Settings > Runners > Register Runner."
      ask_secret RUNNER_TOKEN "Runner token"
      [[ -n "${RUNNER_TOKEN}" ]] || die "A runner token is required (--token)."
    fi
  fi
  if [[ -n "${RUNNER_TOKEN}" && ! "${RUNNER_TOKEN}" =~ ^[A-Za-z0-9_-]{16,}$ ]]; then
    die "This does not look like a runner token."
  fi

  BACKEND_URL="${BACKEND_URL:-${current_url}}"
  BACKEND_URL="${BACKEND_URL%/}"
  [[ "${BACKEND_URL}" =~ ^https?://[A-Za-z0-9.:_-]+$ ]] ||
    die "Invalid control plane URL: '${BACKEND_URL}' (use https://host[:port])."
}

check_runner_host() {
  [[ "$(uname -m)" == "x86_64" ]] || die "The runner needs an x86_64 host."
  [[ -e /dev/kvm ]] ||
    die "/dev/kvm is missing. Enable hardware (or nested) virtualization for this host."
  command -v apt-get >/dev/null || die "The runner installer supports Debian and Ubuntu (apt)."
  command -v systemctl >/dev/null || die "The runner needs systemd."
}

check_ports_free() {
  [[ "${SERVER_EXISTS}" -eq 0 ]] && command -v ss >/dev/null || return 0
  local port
  for port in 80 443; do
    if ss -Hltn "( sport = :${port} )" | grep -q .; then
      die "Port ${port} is already in use. Free it, or run the control plane behind your own reverse proxy (README)."
    fi
  done
}

summarize_plan() {
  step "Plan"
  case "${MODE}" in
    full) info "Control plane and runner on this host (${INSTALL_DIR})" ;;
    server) info "Control plane only (${INSTALL_DIR})" ;;
    runner) info "Runner only (${INSTALL_DIR}/runner), connecting to ${BACKEND_URL}" ;;
  esac
  if [[ "${MODE}" != "runner" ]]; then
    if [[ "${SERVER_EXISTS}" -eq 1 ]]; then
      info "Existing control plane: update images ${VERSION}, keep configuration and data"
    else
      info "Public URL: $(public_url_for "${DOMAIN}")"
    fi
    [[ "${SERVER_READY}" -eq 1 ]] || info "Admin: ${ADMIN_EMAIL}"
  fi
  [[ "${MODE}" == "server" || "${RUNNER_EXISTS}" -eq 0 ]] ||
    warn "Updating restarts the runner; running agent commands are interrupted."
  confirm "Continue?" || die "Aborted."
}

public_url_for() {
  if [[ "$1" =~ ^[0-9.]+$ || "$1" != *.* ]]; then
    printf 'http://%s' "$1"
  else
    printf 'https://%s' "$1"
  fi
}

# -- source --------------------------------------------------------------------

fetch_source() {
  local ref="${VERSION}"
  [[ "${ref}" != "latest" ]] || ref="main"
  local url="${OPENCURIA_SOURCE_URL:-https://github.com/${REPO}/archive/${ref}.tar.gz}"
  WORK_DIR="$(mktemp -d)"
  info "Downloading ${url}"
  curl -fsSL "${url}" | tar -xz -C "${WORK_DIR}" --strip-components=1 ||
    die "Could not download OpenCuria ${ref}."
  SRC_DIR="${WORK_DIR}"
}

# -- control plane -------------------------------------------------------------

ensure_docker() {
  if command -v docker >/dev/null; then
    docker compose version >/dev/null 2>&1 ||
      die "Docker is installed without the Compose plugin. Install docker-compose-plugin and re-run."
    return
  fi
  confirm "Docker is not installed. Install it with the official script from get.docker.com?" ||
    die "Docker is required for the control plane."
  step "Installing Docker"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker >/dev/null 2>&1 || true
}

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
EOF
}

install_control_plane() {
  step "Setting up the control plane in ${INSTALL_DIR}"
  install -d -m 755 "${INSTALL_DIR}"
  install -d -m 700 "${INSTALL_DIR}/secrets" "${INSTALL_DIR}/secrets/push"
  install -m 644 "${SRC_DIR}/compose.yml" "${INSTALL_DIR}/compose.yml"
  install -m 644 "${SRC_DIR}/Caddyfile" "${INSTALL_DIR}/Caddyfile"
  if [[ "${SERVER_EXISTS}" -eq 0 ]]; then
    write_server_env
  else
    set_env_value "${INSTALL_DIR}/.env" OPENCURIA_TAG "${VERSION}"
  fi

  step "Pulling images"
  compose pull
  step "Starting services"
  compose up -d --remove-orphans --wait --wait-timeout 600 ||
    die "Services did not become healthy. Inspect: cd ${INSTALL_DIR} && docker compose logs"

  if [[ "${SERVER_READY}" -eq 0 ]]; then
    bootstrap_control_plane
  fi
  PUBLIC_URL="$(env_value "${INSTALL_DIR}/.env" PUBLIC_URL)"
}

bootstrap_control_plane() {
  step "Creating the admin account"
  local args=(--admin-email "${ADMIN_EMAIL}")
  if [[ "${MODE}" == "full" ]]; then
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

# -- runner --------------------------------------------------------------------

# Docker sets the iptables FORWARD policy to DROP. libvirt's nftables backend
# cannot override that verdict, which cuts workspace VMs off the network.
pin_libvirt_firewall_backend() {
  local conf=/etc/libvirt/network.conf
  [[ -f "${conf}" ]] && grep -q firewall_backend "${conf}" || return 1
  grep -Eq '^[[:space:]]*firewall_backend[[:space:]]*=' "${conf}" && return 1
  printf '\n# Set by the OpenCuria installer: works next to Docker.\nfirewall_backend = "iptables"\n' >>"${conf}"
}

install_runner() {
  local dir="${INSTALL_DIR}/runner" new="${INSTALL_DIR}/runner.new" python_ok

  step "Installing QEMU/KVM and build dependencies"
  apt-get update -qq
  # needrestart would otherwise restart services such as Docker mid-install.
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get install -y -qq \
    qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients genisoimage \
    ovmf swtpm-tools iptables openssh-client build-essential pkg-config \
    libvirt-dev python3-dev python3-venv >/dev/null
  python_ok="$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')"
  [[ "${python_ok}" == "1" ]] || die "The runner needs Python 3.10 or newer."

  if pin_libvirt_firewall_backend; then
    systemctl restart libvirtd
  fi
  systemctl enable --now libvirtd >/dev/null
  install -d -m 755 /var/lib/opencuria \
    /var/lib/opencuria/{images,base-images,disks,snapshots}

  step "Installing the runner in ${dir}"
  rm -rf "${new}"
  install -d -m 755 "${INSTALL_DIR}"
  cp -a "${SRC_DIR}/runner" "${new}"
  rm -rf "${new}/.venv" "${new}/.env"
  if [[ -d "${dir}/.venv" ]]; then mv "${dir}/.venv" "${new}/.venv"; fi
  if [[ -f "${dir}/.env" ]]; then cp -p "${dir}/.env" "${new}/.env"; fi
  rm -rf "${dir}"
  mv "${new}" "${dir}"

  if [[ ! -x "${dir}/.venv/bin/python" ]]; then
    python3 -m venv "${dir}/.venv"
  fi
  "${dir}/.venv/bin/pip" install --quiet --upgrade pip
  "${dir}/.venv/bin/pip" install --quiet -r "${dir}/requirements.txt"

  if [[ ! -f "${dir}/.env" ]]; then
    install -m 600 "${dir}/.env.example" "${dir}/.env"
  fi
  chmod 600 "${dir}/.env"
  set_env_value "${dir}/.env" RUNNER_BACKEND_URL "${BACKEND_URL}"
  if [[ -n "${RUNNER_TOKEN}" ]]; then
    set_env_value "${dir}/.env" RUNNER_API_TOKEN "${RUNNER_TOKEN}"
  fi

  sed "s|__RUNNER_DIR__|${dir}|g" "${dir}/systemd/${RUNNER_SERVICE}.service" \
    >"/etc/systemd/system/${RUNNER_SERVICE}.service"
  systemctl daemon-reload
  systemctl enable --quiet "${RUNNER_SERVICE}"

  step "Starting the runner"
  local since
  since="$(date +%s)"
  systemctl restart "${RUNNER_SERVICE}"
  wait_for_runner "${since}"
}

wait_for_runner() {
  local deadline=$((SECONDS + 120)) logs
  while ((SECONDS < deadline)); do
    logs="$(journalctl -u "${RUNNER_SERVICE}" --since "@$1" -o cat --no-pager 2>/dev/null || true)"
    if grep -q websocket_connected <<<"${logs}"; then
      info "Runner connected to ${BACKEND_URL}"
      return 0
    fi
    if grep -q websocket_connect_error <<<"${logs}"; then
      grep websocket_connect_error <<<"${logs}" | tail -n 1 >&2
      die "The control plane rejected the runner. Check the token and re-run."
    fi
    sleep 3
  done
  journalctl -u "${RUNNER_SERVICE}" --since "@$1" -n 20 --no-pager >&2 || true
  die "The runner did not connect within 2 minutes. Logs: journalctl -u ${RUNNER_SERVICE} -f"
}

check_backend_reachable() {
  curl -fsS --max-time 15 -o /dev/null "${BACKEND_URL}/api/v1/health/" ||
    die "Cannot reach ${BACKEND_URL}/api/v1/health/ from this host."
}

# -- summary -------------------------------------------------------------------

print_summary() {
  printf '\n%sOpenCuria is ready.%s\n\n' "${C_GREEN}${C_BOLD}" "${C_RESET}"
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
  if [[ "${MODE}" == "server" ]]; then
    printf '  - Add a runner: Settings > Runners > Register Runner shows the command\n'
    printf '    to run on a Linux host with KVM.\n'
  fi
  if [[ "${MODE}" != "runner" && "${PUBLIC_URL}" == https://* ]]; then
    printf '  - HTTPS needs DNS for %s pointing here and ports 80/443 reachable.\n' \
      "${PUBLIC_URL#https://}"
  fi
  printf '  - Build an image under Settings > Image Definitions, then create a workspace.\n'
  printf '  - Update: run the install command again.\n'
}

main() {
  parse_args "$@"
  [[ "${EUID}" -eq 0 ]] || die "Run as root: curl -fsSL ${INSTALL_URL} | sudo bash"
  detect_state
  printf '%sOpenCuria installer%s\n' "${C_BOLD}" "${C_RESET}" >&2
  choose_mode

  if [[ "${MODE}" != "server" ]]; then check_runner_host; fi
  if [[ "${MODE}" != "runner" ]]; then
    collect_server_settings
    check_ports_free
  fi
  if [[ "${MODE}" != "server" ]]; then collect_runner_settings; fi
  if [[ "${MODE}" == "runner" ]]; then check_backend_reachable; fi
  summarize_plan

  trap cleanup EXIT
  step "Fetching OpenCuria ${VERSION}"
  fetch_source

  if [[ "${MODE}" != "runner" ]]; then
    ensure_docker
    install_control_plane
  fi
  if [[ "${MODE}" != "server" ]]; then install_runner; fi
  print_summary
}

main "$@"
