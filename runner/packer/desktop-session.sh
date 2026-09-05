#!/usr/bin/env bash
# --- QEMU KasmVNC desktop session (XFCE + WhiteSur) ---
# Keep these two files byte-identical:
#   backend/apps/runners/scripts/qemu_desktop_session.sh
#   runner/packer/desktop-session.sh
#
# Provisions an X11 desktop for KasmVNC: XFCE, WhiteSur theme, Plank dock,
# Ventura-style wallpaper, rofi launcher, and skippy-xd Exposé. Chrome is a
# dock launcher only — it does not auto-start.
set -euo pipefail

echo "=== Installing KasmVNC desktop session support (XFCE + WhiteSur) ==="

export DEBIAN_FRONTEND=noninteractive
export HOME=/root

apt-get update
apt-get install -y --no-install-recommends \
    xfonts-base \
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
    libcairo2 \
    wget \
    ca-certificates \
    git \
    xfce4 \
    xfce4-session \
    xfce4-panel \
    xfce4-settings \
    xfdesktop4 \
    xfwm4 \
    xfce4-terminal \
    xfce4-whiskermenu-plugin \
    xfce4-notifyd \
    thunar \
    plank \
    rofi \
    gtk2-engines-murrine \
    sassc \
    libglib2.0-dev-bin \
    libxml2-utils \
    libgtk-3-bin \
    fonts-inter \
    fonts-noto-color-emoji \
    adwaita-icon-theme \
    procps \
    psmisc \
    ffmpeg \
    xdotool
apt-get install -y libasound2t64 || apt-get install -y libasound2

# Exposé: Ubuntu 22.04 ships skippy-xd; 24.04 does not.
if ! apt-get install -y skippy-xd; then
    echo "skippy-xd is not in apt; building from source"
    apt-get install -y --no-install-recommends \
        gcc make pkg-config \
        libx11-dev libxft-dev libxrender-dev libxcomposite-dev \
        libxdamage-dev libxfixes-dev libxext-dev libxinerama-dev \
        libpng-dev zlib1g-dev libjpeg-dev libgif-dev
    git clone --depth=1 --branch v2025.11.30 \
        https://github.com/felixfung/skippy-xd.git /tmp/skippy-xd
    make -C /tmp/skippy-xd
    make -C /tmp/skippy-xd install
    rm -rf /tmp/skippy-xd
fi

wget -q -O /tmp/kasmvnc.deb \
    "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_jammy_1.3.3_amd64.deb"
apt-get install -y /tmp/kasmvnc.deb || true
apt-get install -f -y
rm -f /tmp/kasmvnc.deb

# Real Chrome .deb — Ubuntu Chromium packages route through snapd.
wget -q -O /tmp/google-chrome.deb \
    https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y /tmp/google-chrome.deb || apt-get install -f -y
rm -f /tmp/google-chrome.deb

mkdir -p /root/.vnc
touch /root/.vnc/.de-was-selected
printf "password\npassword\n" | vncpasswd -u root -w -r 2>/dev/null || true

cat >/root/.vnc/kasmvnc.yaml <<'KASMCFG'
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

cat >/usr/local/bin/opencuria-desktop-browser <<'BROWSER'
#!/bin/bash
set -eu
for browser in google-chrome-stable google-chrome chromium chromium-browser /usr/lib/chromium/chromium; do
    if [ "${browser#/}" != "$browser" ]; then
        if [ -x "$browser" ]; then
            exec "$browser" --no-sandbox --disable-gpu \
                --disable-dev-shm-usage --no-first-run
        fi
        continue
    fi
    if command -v "$browser" >/dev/null 2>&1; then
        if [ "$browser" = "chromium-browser" ] && ! chromium-browser --version >/dev/null 2>&1; then
            continue
        fi
        exec "$browser" --no-sandbox --disable-gpu \
            --disable-dev-shm-usage --no-first-run
    fi
done
echo "No supported browser binary found for desktop session" >&2
exit 1
BROWSER
chmod +x /usr/local/bin/opencuria-desktop-browser

cat >/usr/share/applications/opencuria-chrome.desktop <<'CHROME_DESKTOP'
[Desktop Entry]
Type=Application
Name=Chrome
Comment=OpenCuria workspace browser
Exec=/usr/local/bin/opencuria-desktop-browser
Icon=google-chrome
Terminal=false
Categories=Network;WebBrowser;
StartupNotify=true
CHROME_DESKTOP

# --- WhiteSur theme, icons, cursors, wallpaper (pinned, MIT) ---
THEME_WORKDIR=/tmp/opencuria-desktop-theme
rm -rf "$THEME_WORKDIR"
mkdir -p "$THEME_WORKDIR"

git clone --depth=1 --branch 2026-08-08 \
    https://github.com/vinceliuice/WhiteSur-gtk-theme.git \
    "$THEME_WORKDIR/gtk"
"$THEME_WORKDIR/gtk/install.sh" \
    -d /usr/share/themes \
    -n WhiteSur \
    -c Light \
    -o normal \
    --silent-mode

git clone --depth=1 --branch 2026-08-11 \
    https://github.com/vinceliuice/WhiteSur-icon-theme.git \
    "$THEME_WORKDIR/icons"
"$THEME_WORKDIR/icons/install.sh" -d /usr/share/icons -t default

git clone --depth=1 \
    https://github.com/vinceliuice/WhiteSur-cursors.git \
    "$THEME_WORKDIR/cursors"
# install.sh copies ./dist relative to CWD, not the script path.
test -d "$THEME_WORKDIR/cursors/dist"
( cd "$THEME_WORKDIR/cursors" && ./install.sh )

mkdir -p /usr/share/backgrounds/opencuria
wget -q -O /usr/share/backgrounds/opencuria/Ventura-light.jpg \
    https://raw.githubusercontent.com/vinceliuice/WhiteSur-wallpapers/master/4k/Ventura-light.jpg
test -s /usr/share/backgrounds/opencuria/Ventura-light.jpg
test -d /usr/share/themes/WhiteSur-Light
test -d /usr/share/icons/WhiteSur
test -d /usr/share/icons/WhiteSur-cursors

rm -rf "$THEME_WORKDIR"

if [ -d /usr/share/plank/themes/WhiteSur ]; then
    PLANK_THEME=WhiteSur
elif [ -d /usr/share/plank/themes/WhiteSur-light ]; then
    PLANK_THEME=WhiteSur-light
else
    PLANK_THEME=Default
fi

# --- XFCE / Plank / rofi config (no xfconf-query: no display at image build) ---
mkdir -p \
    /root/.config/xfce4/xfconf/xfce-perchannel-xml \
    /root/.config/xfce4/autostart \
    /root/.config/autostart \
    /root/.config/plank/dock1/launchers \
    /root/.config/rofi \
    /root/.config/gtk-3.0 \
    /root/.config/gtk-4.0 \
    /etc/xdg/autostart

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfwm4" version="1.0">
  <property name="general" type="empty">
    <property name="theme" type="string" value="WhiteSur-Light"/>
    <property name="button_layout" type="string" value="CHM|"/>
    <property name="title_alignment" type="string" value="center"/>
    <property name="title_font" type="string" value="Inter 11"/>
    <property name="use_compositing" type="bool" value="true"/>
    <property name="show_dock_shadow" type="bool" value="true"/>
    <property name="show_frame_shadow" type="bool" value="true"/>
    <property name="show_popup_shadow" type="bool" value="true"/>
    <property name="wrap_windows" type="bool" value="false"/>
    <property name="snap_to_border" type="bool" value="true"/>
    <property name="snap_to_windows" type="bool" value="true"/>
    <property name="tile_on_move" type="bool" value="true"/>
    <property name="box_move" type="bool" value="false"/>
    <property name="box_resize" type="bool" value="false"/>
    <property name="click_to_focus" type="bool" value="true"/>
    <property name="raise_on_click" type="bool" value="true"/>
    <property name="raise_on_focus" type="bool" value="false"/>
    <property name="zoom_desktop" type="bool" value="false"/>
    <property name="scroll_workspaces" type="bool" value="false"/>
  </property>
</channel>
XML

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="WhiteSur-Light"/>
    <property name="IconThemeName" type="string" value="WhiteSur"/>
  </property>
  <property name="Gtk" type="empty">
    <property name="CursorThemeName" type="string" value="WhiteSur-cursors"/>
    <property name="CursorThemeSize" type="int" value="24"/>
    <property name="FontName" type="string" value="Inter 11"/>
    <property name="DecorationLayout" type="string" value="close,minimize,maximize:"/>
  </property>
  <property name="Xft" type="empty">
    <property name="Antialias" type="int" value="1"/>
    <property name="Hinting" type="int" value="1"/>
    <property name="HintStyle" type="string" value="hintslight"/>
    <property name="RGBA" type="string" value="rgb"/>
  </property>
</channel>
XML

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="desktop-icons" type="empty">
    <property name="style" type="int" value="0"/>
    <property name="file-icons" type="empty">
      <property name="show-home" type="bool" value="false"/>
      <property name="show-filesystem" type="bool" value="false"/>
      <property name="show-removable" type="bool" value="false"/>
      <property name="show-trash" type="bool" value="false"/>
    </property>
  </property>
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitor0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string" value="/usr/share/backgrounds/opencuria/Ventura-light.jpg"/>
        </property>
      </property>
      <property name="monitorVNC-0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string" value="/usr/share/backgrounds/opencuria/Ventura-light.jpg"/>
        </property>
      </property>
      <property name="monitorDVI-D-0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="5"/>
          <property name="last-image" type="string" value="/usr/share/backgrounds/opencuria/Ventura-light.jpg"/>
        </property>
      </property>
    </property>
  </property>
</channel>
XML

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-panel" version="1.0">
  <property name="configver" type="int" value="2"/>
  <property name="panels" type="array">
    <value type="int" value="1"/>
    <property name="dark-mode" type="bool" value="false"/>
    <property name="panel-1" type="empty">
      <property name="position" type="string" value="p=6;x=960;y=0"/>
      <property name="length" type="uint" value="100"/>
      <property name="position-locked" type="bool" value="true"/>
      <property name="icon-size" type="uint" value="16"/>
      <property name="size" type="uint" value="28"/>
      <property name="background-style" type="uint" value="1"/>
      <property name="background-rgba" type="array">
        <value type="double" value="0.96"/>
        <value type="double" value="0.96"/>
        <value type="double" value="0.96"/>
        <value type="double" value="0.92"/>
      </property>
      <property name="plugin-ids" type="array">
        <value type="int" value="1"/>
        <value type="int" value="2"/>
        <value type="int" value="3"/>
        <value type="int" value="4"/>
      </property>
    </property>
  </property>
  <property name="plugins" type="empty">
    <property name="plugin-1" type="string" value="whiskermenu">
      <property name="button-title" type="string" value="Applications"/>
      <property name="show-button-title" type="bool" value="true"/>
    </property>
    <property name="plugin-2" type="string" value="separator">
      <property name="expand" type="bool" value="true"/>
      <property name="style" type="uint" value="0"/>
    </property>
    <property name="plugin-3" type="string" value="clock">
      <property name="mode" type="uint" value="2"/>
      <property name="digital-layout" type="uint" value="3"/>
      <property name="digital-time-format" type="string" value="%a %H:%M"/>
    </property>
    <property name="plugin-4" type="string" value="systray">
      <property name="square-icons" type="bool" value="true"/>
      <property name="symbolic-icons" type="bool" value="true"/>
    </property>
  </property>
</channel>
XML

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0">
  <property name="general" type="empty">
    <property name="FailsafeSessionName" type="string" value="Failsafe"/>
    <property name="SaveOnExit" type="bool" value="false"/>
  </property>
  <property name="splash" type="empty">
    <property name="Engine" type="string" value=""/>
  </property>
  <property name="startup" type="empty">
    <property name="screensaver" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
</channel>
XML

cat >/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-keyboard-shortcuts" version="1.0">
  <property name="commands" type="empty">
    <property name="custom" type="empty">
      <property name="override" type="bool" value="true"/>
      <property name="&lt;Super&gt;space" type="string" value="rofi -show drun -show-icons"/>
      <property name="&lt;Super&gt;Tab" type="string" value="/usr/local/bin/opencuria-expose"/>
    </property>
  </property>
  <property name="xfwm4" type="empty">
    <property name="custom" type="empty">
      <property name="override" type="bool" value="true"/>
      <property name="&lt;Super&gt;Left" type="string" value="tile_left_key"/>
      <property name="&lt;Super&gt;Right" type="string" value="tile_right_key"/>
      <property name="&lt;Super&gt;Up" type="string" value="tile_up_key"/>
      <property name="&lt;Super&gt;Down" type="string" value="tile_down_key"/>
      <property name="&lt;Alt&gt;Tab" type="string" value="cycle_windows_key"/>
      <property name="&lt;Alt&gt;&lt;Shift&gt;Tab" type="string" value="cycle_reverse_windows_key"/>
      <property name="&lt;Alt&gt;F4" type="string" value="close_window_key"/>
    </property>
  </property>
</channel>
XML

cat >/root/.gtkrc-2.0 <<'GTK2'
gtk-theme-name="WhiteSur-Light"
gtk-icon-theme-name="WhiteSur"
gtk-cursor-theme-name="WhiteSur-cursors"
gtk-font-name="Inter 11"
GTK2

cat >/root/.config/gtk-3.0/settings.ini <<'GTK3'
[Settings]
gtk-theme-name=WhiteSur-Light
gtk-icon-theme-name=WhiteSur
gtk-cursor-theme-name=WhiteSur-cursors
gtk-font-name=Inter 11
gtk-decoration-layout=close,minimize,maximize:
GTK3

cat >/root/.config/gtk-4.0/settings.ini <<'GTK4'
[Settings]
gtk-theme-name=WhiteSur-Light
gtk-icon-theme-name=WhiteSur
gtk-cursor-theme-name=WhiteSur-cursors
gtk-font-name=Inter 11
gtk-decoration-layout=close,minimize,maximize:
GTK4

cat >/root/.config/rofi/config.rasi <<'ROFI'
configuration {
  modi: "drun,run";
  show-icons: true;
  font: "Inter 12";
  matching: "fuzzy";
}
ROFI

cat >/root/.config/plank/dock1/settings <<PLANK
[PlankDockPreferences]
HideMode=0
IconSize=48
UnhideDelay=0
HideDelay=2000
Monitor=-1
DockItems=opencuria-chrome.dockitem;;thunar.dockitem;;xfce4-terminal.dockitem
Position=3
Alignment=3
ItemsAlignment=3
PinnedOnly=true
PressureReveal=false
ShowDockItem=false
ZoomEnabled=true
ZoomPercent=150
Theme=${PLANK_THEME}
PLANK

cat >/root/.config/plank/dock1/launchers/opencuria-chrome.dockitem <<'DOCK'
[PlankDockItemPreferences]
Launcher=file:///usr/share/applications/opencuria-chrome.desktop
DOCK

cat >/root/.config/plank/dock1/launchers/thunar.dockitem <<'DOCK'
[PlankDockItemPreferences]
Launcher=file:///usr/share/applications/thunar.desktop
DOCK

cat >/root/.config/plank/dock1/launchers/xfce4-terminal.dockitem <<'DOCK'
[PlankDockItemPreferences]
Launcher=file:///usr/share/applications/xfce4-terminal.desktop
DOCK

cat >/usr/local/bin/opencuria-expose <<'EXPOSE'
#!/bin/bash
# Super+Tab Exposé. Supports Ubuntu's skippy-xd and the newer --expose CLI.
if skippy-xd --help 2>&1 | grep -q -- '--expose'; then
    if ! pgrep -x skippy-xd >/dev/null 2>&1; then
        skippy-xd --start-daemon >/dev/null 2>&1 || true
        sleep 0.2
    fi
    exec skippy-xd --expose
fi
exec skippy-xd
EXPOSE
chmod +x /usr/local/bin/opencuria-expose

cat >/usr/local/bin/opencuria-desktop-apply-theme <<'APPLY'
#!/bin/bash
set -eu
export DISPLAY="${DISPLAY:-:1}"
export HOME=/root
WALLPAPER="/usr/share/backgrounds/opencuria/Ventura-light.jpg"
sleep 2
if ! command -v xfconf-query >/dev/null 2>&1; then
    exit 0
fi
props="$(xfconf-query -c xfce4-desktop -l 2>/dev/null | grep last-image || true)"
if [ -n "$props" ]; then
    while IFS= read -r prop; do
        [ -n "$prop" ] || continue
        xfconf-query -c xfce4-desktop -p "$prop" -s "$WALLPAPER" --create -t string || true
    done <<EOF
$props
EOF
fi
xfconf-query -c xfwm4 -p /general/theme -s "WhiteSur-Light" || true
xfconf-query -c xfwm4 -p /general/button_layout -s "CHM|" || true
xfconf-query -c xfwm4 -p /general/use_compositing --create -t bool -s true || true
xfconf-query -c xsettings -p /Net/ThemeName -s "WhiteSur-Light" || true
xfconf-query -c xsettings -p /Net/IconThemeName -s "WhiteSur" || true
xfconf-query -c xsettings -p /Gtk/CursorThemeName -s "WhiteSur-cursors" || true
xfconf-query -c xsettings -p /Gtk/FontName -s "Inter 11" || true
APPLY
chmod +x /usr/local/bin/opencuria-desktop-apply-theme

cat >/root/.config/autostart/plank.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Plank
Exec=plank
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
DESKTOP

cat >/root/.config/autostart/opencuria-desktop-apply-theme.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=OpenCuria desktop theme
Exec=/usr/local/bin/opencuria-desktop-apply-theme
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
DESKTOP

cat >/root/.config/autostart/skippy-xd.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=skippy-xd
Exec=sh -c 'skippy-xd --start-daemon 2>/dev/null || true'
X-GNOME-Autostart-enabled=true
OnlyShowIn=XFCE;
DESKTOP

# Hide noisy first-login helpers that expect a local seat.
for name in \
    xfce4-power-manager \
    xfce4-screensaver \
    light-locker \
    xscreensaver \
    gnome-keyring-pkcs11 \
    gnome-keyring-secrets \
    gnome-keyring-ssh \
    polkit-gnome-authentication-agent-1 \
    xfce4-tips \
    update-notifier \
    nm-applet \
    blueman \
    pulseaudio
do
    printf '%s\n' '[Desktop Entry]' 'Hidden=true' \
        >"/root/.config/autostart/${name}.desktop"
done

cat >/root/.vnc/xstartup <<'XSTARTUP'
#!/bin/bash
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
exec dbus-launch --exit-with-session startxfce4
XSTARTUP
chmod +x /root/.vnc/xstartup

cat >/usr/local/bin/opencuria-desktop-start <<'DESKSTART'
#!/bin/bash
set -e
export DISPLAY=:1
export HOME=/root
/usr/local/bin/opencuria-desktop-stop 2>/dev/null || true
mkdir -p /root/.vnc
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# Launch Xvnc directly (bypasses KasmVNC perl wrapper which prompts for user input)
/usr/bin/Xvnc :1 \
    -geometry 1920x1080 \
    -depth 24 \
    -rfbport 5901 \
    -SecurityTypes None \
    -disableBasicAuth \
    -websocketPort 6901 \
    -httpd /usr/share/kasmvnc/www \
    -interface 0.0.0.0 \
    -AlwaysShared \
    -AcceptKeyEvents \
    -AcceptPointerEvents \
    -AcceptSetDesktopSize \
    -SendCutText \
    -AcceptCutText \
    >>/root/.vnc/server.log 2>&1 &

for _ in $(seq 1 120); do
    if [ -e /tmp/.X11-unix/X1 ]; then
        /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &
        echo "Desktop session started on :1 (ws port 6901)"
        exit 0
    fi
    sleep 0.25
done
echo "Desktop session failed to start" >&2
exit 1
DESKSTART

cat >/usr/local/bin/opencuria-desktop-stop <<'DESKSTOP'
#!/bin/bash
# Stop Xvnc and the XFCE session (including Plank / skippy-xd).
for pattern in \
    'Xvnc.*:1' \
    'Xtigervnc.*:1' \
    'xfce4-session' \
    'xfwm4' \
    'xfce4-panel' \
    'xfdesktop' \
    'plank' \
    'skippy-xd' \
    'rofi'
do
    for pid in $(pgrep -f "$pattern" 2>/dev/null); do
        kill "$pid" 2>/dev/null || true
    done
done
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
DESKSTOP

chmod +x /usr/local/bin/opencuria-desktop-start /usr/local/bin/opencuria-desktop-stop
rm -rf /var/lib/apt/lists/*

echo "=== XFCE + WhiteSur desktop session installed ==="
