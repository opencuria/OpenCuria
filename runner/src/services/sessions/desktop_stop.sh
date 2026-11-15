#!/bin/bash
# Stop only desktop executable names; agent argv can contain words like profile.
for pattern in \
    '^(/[^[:space:]]*/)?(Xvnc|Xtigervnc) :1([[:space:]]|$)' \
    '^(/[^[:space:]]*/)?(xfce4-session|xfwm4|xfce4-panel|xfdesktop|plank|skippy-xd|rofi|openbox)([[:space:]]|$)'
do
    for pid in $(pgrep -f -- "$pattern" 2>/dev/null); do
        kill "$pid" 2>/dev/null || true
    done
done
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
