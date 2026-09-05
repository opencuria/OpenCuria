#!/usr/bin/env bash
# init.sh — Provision script for the QEMU/KVM workspace base image.
#
# Tooling mirrors the Docker workspace image. The desktop session is XFCE +
# WhiteSur (see desktop-session.sh); Docker workspaces keep Openbox + Chrome.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "=== Installing system dependencies ==="
apt-get update
apt-get install -y \
    curl \
    wget \
    git \
    openssh-client \
    openssh-server \
    build-essential \
    ca-certificates \
    gnupg \
    lsb-release \
    software-properties-common \
    python3 \
    python3-pip \
    python3-venv \
    vim \
    nano \
    jq \
    zip \
    unzip \
    tini

echo "=== Installing Node.js 22.x ==="
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
npm install -g npm@latest

echo "=== Installing GitHub CLI ==="
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
apt-get update
apt-get install -y gh

# Enable SSH server for asyncssh access from the runner
systemctl enable ssh

echo "=== Configuring ext4 error behavior ==="
# Never remount read-only on errors — workspaces must stay writable.
# 'continue' logs the error but keeps the filesystem mounted read-write.
tune2fs -e continue /dev/vda1

echo "=== Installing KasmVNC desktop session support ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_SCRIPT="${SCRIPT_DIR}/desktop-session.sh"
if [ ! -f "$DESKTOP_SCRIPT" ]; then
    echo "Missing QEMU desktop session script: $DESKTOP_SCRIPT" >&2
    exit 1
fi
bash "$DESKTOP_SCRIPT"

echo "=== Cleanup ==="
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "=== Base image provisioning complete ==="
