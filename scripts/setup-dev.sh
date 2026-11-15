#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

print_section() {
  local title="$1"
  printf '\n==> %s\n' "$title"
}

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  fi
}

setup_python_component() {
  local component="$1"
  local component_dir="$REPO_ROOT/$component"

  print_section "Setting up ${component} Python environment"

  if [[ ! -d "$component_dir/.venv" ]]; then
    python3 -m venv "$component_dir/.venv"
  fi

  (
    cd "$component_dir"
    source .venv/bin/activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
  )
}

setup_node_component() {
  local component="$1"
  local component_dir="$REPO_ROOT/$component"

  print_section "Installing ${component} Node dependencies"
  (
    cd "$component_dir"
    npm ci
  )
}

run_backend_migrations() {
  print_section "Applying backend migrations"
  (
    cd "$REPO_ROOT/backend"
    source .venv/bin/activate
    python manage.py migrate
  )
}

require_command python3
require_command npm

install_system_deps() {
  print_section "Installing system build dependencies"

  if ! command -v apt-get >/dev/null 2>&1; then
    printf 'apt-get not found — skipping system dependency install (non-Debian system).\n'
    return 0
  fi

  local -a apt_prefix=()
  if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      apt_prefix=(sudo)
    else
      printf 'Skipping system dependency install: root or sudo is required for apt-get.\n'
      return 0
    fi
  fi

  # Required to compile libvirt-python and other C-extension packages.
  "${apt_prefix[@]}" apt-get update
  "${apt_prefix[@]}" apt-get install -y \
    build-essential \
    python3-dev \
    pkg-config \
    libvirt-dev \
    qemu-kvm \
    qemu-utils \
    libvirt-daemon-system \
    libvirt-clients \
    genisoimage
}

install_system_deps
setup_python_component backend
setup_python_component runner
setup_node_component .
run_backend_migrations

print_section "Setup complete"
printf 'Configure runner/.env and start the native QEMU runner.\n'
