#!/bin/bash
# Start x11vnc attached to whatever display the logged-in desktop is actually using.
# The display number (:0 vs :1) and the Xauthority path are not stable across logins,
# so we read them straight from the running gnome-shell / desktop process instead of guessing.
set -u
USER_NAME="${SUDO_USER:-saibala}"

for _ in $(seq 1 60); do
    PID="$(pgrep -u "$USER_NAME" -x gnome-shell | head -1)"
    [ -z "$PID" ] && PID="$(pgrep -u "$USER_NAME" -x cinnamon 2>/dev/null | head -1)"
    [ -z "$PID" ] && PID="$(pgrep -u "$USER_NAME" -x xfwm4 2>/dev/null | head -1)"
    if [ -n "$PID" ] && [ -r "/proc/$PID/environ" ]; then
        DISPLAY="$(tr '\0' '\n' < "/proc/$PID/environ" | sed -n 's/^DISPLAY=//p' | head -1)"
        XAUTH="$(tr '\0' '\n' < "/proc/$PID/environ" | sed -n 's/^XAUTHORITY=//p' | head -1)"
        if [ -n "$DISPLAY" ] && [ -n "$XAUTH" ] && [ -r "$XAUTH" ]; then
            echo "x11vnc attaching to DISPLAY=$DISPLAY AUTH=$XAUTH"
            # Speed: xdamage = only changed regions are re-read; -threads uses more cores; low -defer/-wait = lower latency;
            # -nowf skips wireframe tricks; -speeds lan skips bandwidth probing. X11VNC_SCALE (e.g. 0.75) downsamples
            # server-side before sending, the single biggest win on slow links (":nb" = no blending, fastest).
            SCALE_ARGS=""
            [ -n "${X11VNC_SCALE:-}" ] && SCALE_ARGS="-scale ${X11VNC_SCALE}:nb"
            exec x11vnc -display "$DISPLAY" -auth "$XAUTH" \
                 -localhost -forever -shared -nopw -quiet -repeat \
                 -xdamage -threads -defer 5 -wait 5 -nowf -speeds lan $SCALE_ARGS
        fi
    fi
    sleep 2
done
echo "x11vnc-start: no graphical session found after 2 minutes" >&2
exit 1
