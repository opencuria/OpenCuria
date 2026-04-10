#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_WORKSPACE_IMAGE=0

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

setup_git_hooks() {
  print_section "Configuring git hooks"
  git -C "$REPO_ROOT" config core.hooksPath .githooks
}

build_workspace_image() {
  print_section "Building local workspace image"
  docker build \
    -t opencuria/workspace:latest \
    "$REPO_ROOT/runner" \
    -f "$REPO_ROOT/runner/Dockerfile"
}

print_next_steps() {
  print_section "Setup complete"
  printf 'Development environment is ready.\n'
  printf 'Git hooks are active and commits now require ./.githooks/pre-commit to pass.\n'
  printf 'If you need the local runner workspace image as well, rerun with --build-workspace-image.\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-workspace-image)
      BUILD_WORKSPACE_IMAGE=1
      shift
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 1
      ;;
  esac
done

require_command python3
require_command npm
require_command git

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
    libvirt-dev
}

install_system_deps
setup_git_hooks
setup_python_component backend
setup_python_component runner
setup_node_component webapp
run_backend_migrations

if [[ "$BUILD_WORKSPACE_IMAGE" -eq 1 ]]; then
  require_command docker
  build_workspace_image
fi

print_next_steps
