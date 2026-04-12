#!/usr/bin/env bash
# init.sh — Provision script for the QEMU/KVM workspace base image.
#
# This mirrors the software stack installed in runner/Dockerfile
# (the Docker workspace container) so both runtimes are identical.
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
apt-get update
apt-get install -y \
    xfonts-base \
    openbox \
    dbus-x11 \
    x11-xserver-utils \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2

apt-get install -y libasound2t64 || apt-get install -y libasound2

# Install KasmVNC
wget -q -O /tmp/kasmvnc.deb \
    "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb"
apt-get install -y /tmp/kasmvnc.deb || true
apt-get install -f -y
rm -f /tmp/kasmvnc.deb

# Install a real browser binary. On Ubuntu cloud images the Chromium apt
# packages route through snapd, so use the Chrome .deb directly.
wget -q -O /tmp/google-chrome.deb \
    https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y
rm -f /tmp/google-chrome.deb

# Pre-configure KasmVNC (skip interactive DE selection wizard)
mkdir -p /root/.vnc
touch /root/.vnc/.de-was-selected

# Create a dummy VNC user so KasmVNC doesn't prompt for one
echo -e "password\npassword\n" | vncpasswd -u opencuria -w -r 2>/dev/null || true

# KasmVNC config: HTTP mode, no SSL
cat > /root/.vnc/kasmvnc.yaml <<'KASMCFG'
desktop:
  resolution:
    width: 1920
    height: 1080
  allow_resize: true
network:
  protocol: http
  interface: 0.0.0.0
  websocket_port: 6901
  ssl:
    require_ssl: false
    pem_certificate:
    pem_key:
KASMCFG

# Browser launcher — prefer real binaries and skip the Ubuntu snap wrapper.
cat >/usr/local/bin/opencuria-desktop-browser <<'BROWSER'
#!/bin/bash
set -eu
for browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do
    if [ "${browser#/}" != "$browser" ]; then
        if [ -x "$browser" ]; then
            exec "$browser" --no-sandbox --disable-gpu --start-maximized \
                --disable-dev-shm-usage --no-first-run
        fi
        continue
    fi
    if command -v "$browser" >/dev/null 2>&1; then
        if [ "$browser" = "chromium-browser" ] && ! chromium-browser --version >/dev/null 2>&1; then
            continue
        fi
        exec "$browser" --no-sandbox --disable-gpu --start-maximized \
            --disable-dev-shm-usage --no-first-run
    fi
done
echo "No supported browser binary found for desktop session" >&2
BROWSER

# xstartup — launched by vncserver for each session
cat > /root/.vnc/xstartup <<'XSTARTUP'
#!/bin/bash
export DISPLAY=:1
export HOME=/root
openbox-session &
sleep 1
/usr/local/bin/opencuria-desktop-browser >/root/.vnc/browser.log 2>&1 &
wait
XSTARTUP
chmod +x /root/.vnc/xstartup
chmod +x /usr/local/bin/opencuria-desktop-browser

# Desktop start/stop helper scripts
cat >/usr/local/bin/opencuria-desktop-start <<'SCRIPT'
#!/bin/bash
set -e
export DISPLAY=:1
export HOME=/root

/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true

# Ensure KasmVNC pre-config exists
mkdir -p /root/.vnc
touch /root/.vnc/.de-was-selected

vncserver :1 \
    -geometry 1920x1080 \
    -depth 24 \
    -SecurityTypes None \
    -websocketPort 6901 \
    -disableBasicAuth \
    -interface 0.0.0.0

echo "Desktop session started on :1 (ws port 6901)"
SCRIPT

cat >/usr/local/bin/opencuria-desktop-stop <<'SCRIPT'
#!/bin/bash
HOME=/root vncserver -kill :1 2>/dev/null || true
echo "Desktop session stopped"
SCRIPT

chmod +x /usr/local/bin/opencuria-desktop-start
chmod +x /usr/local/bin/opencuria-desktop-stop

echo "=== Cleanup ==="
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "=== Base image provisioning complete ==="
