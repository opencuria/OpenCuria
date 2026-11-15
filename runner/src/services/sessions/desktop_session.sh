#!/bin/bash
# Canonical desktop startup, refreshed by the runner for existing images too.
set -eu
if [ "${1:-}" != --session-bus ]; then
    unset SESSION_MANAGER
    unset DBUS_SESSION_BUS_ADDRESS
    export DISPLAY=:1
    export HOME=/root
    export XDG_CURRENT_DESKTOP=XFCE
    export XDG_SESSION_DESKTOP=xfce
    export XDG_SESSION_TYPE=x11
    export GTK_THEME=WhiteSur-Light
    export XDG_CONFIG_HOME=/root/.config
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-root}"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
    export NO_AT_BRIDGE=0
    export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
    export QT_ACCESSIBILITY=1
    # GTK2 needs its loadable modules; GTK3/4 provide their own bridge. Keep an
    # explicitly customized module setting and all existing theme preferences.
    if ! grep -q '^[[:space:]]*gtk-modules[[:space:]]*=' /root/.gtkrc-2.0 2>/dev/null; then
        printf '%s\n' 'gtk-modules="gail:atk-bridge"' >>/root/.gtkrc-2.0
    fi
    exec dbus-launch --exit-with-session "$0" --session-bus
fi

# Set both flags before XFCE can launch user apps. Toolkit environment variables
# alone do not activate accessibility for every Chrome, Electron or Qt version.
for property in IsEnabled ScreenReaderEnabled; do
    gdbus call --session --timeout 5 --dest org.a11y.Bus --object-path /org/a11y/bus \
        --method org.freedesktop.DBus.Properties.Set \
        org.a11y.Status "$property" '<true>' >/dev/null
done
touch /root/.vnc/.accessibility-ready
exec startxfce4
