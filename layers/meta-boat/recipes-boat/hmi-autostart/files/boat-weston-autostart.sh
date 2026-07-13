# Installed as /etc/profile.d/boat-weston-autostart.sh.
#
# Launches Weston directly as the console session's own user (BOAT_HMI_USER,
# via getty autologin), NOT via weston-init's weston-start wrapper script.
# CONFIRMED ON HARDWARE: weston-start unconditionally runs weston through
# `su -c ... $WESTON_USER`, which is both unnecessary here (we're already
# the correct user - no privilege change needed) and broken in this setup:
# empty WESTON_USER makes su default to root, and su separately refuses to
# run non-interactively unless it's the foreground process group of the
# tty. The net effect was "su: must be run from a terminal" and, once
# worked around, weston running as root instead of BOAT_HMI_USER. Calling
# weston directly sidesteps all of that.
#
# Deliberately not weston-init's own weston.service either: that unit runs
# Weston as a dedicated "weston" system user, not the UID a container's
# /run/user/<uid> mount is pinned to.

if [ "$(id -u)" = "@BOAT_HMI_UID@" ] && [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    mkdir -p /tmp/.X11-unix

    # weston.ini enables xwayland=true (meta-boat's weston-init bbappend),
    # so X11-only containerized GUI apps (e.g. OpenCPN) can get a display
    # here too - not just Wayland-native ones. XWayland starts
    # asynchronously after Weston itself, so poll briefly for its socket
    # before granting local access (xhost +local: - no auth, matching the
    # common "xhost +" pattern X11-in-docker examples expect; local-only,
    # not exposed over the network). This poller is backgrounded BEFORE the
    # exec below deliberately - see the su note above for why weston itself
    # must stay the foreground replacement, not backgrounded.
    (
        i=0
        while [ "$i" -lt 50 ]; do
            if [ -S /tmp/.X11-unix/X0 ]; then
                DISPLAY=:0 xhost +local: >/dev/null 2>&1
                break
            fi
            i=$((i + 1))
            sleep 0.2
        done
    ) &

    # --use-pixman: CONFIRMED ON HARDWARE - without this, moving the
    # pointer left trailing black boxes behind it on this Jetson (Tegra
    # DRM driver + Weston's GL/GBM renderer's cursor-plane damage tracking
    # - a known class of bug on less-mainstream DRM drivers, not a
    # Chromium/container issue). Forces Weston's CPU (pixman) renderer,
    # which does full-region repaints instead of relying on a hardware
    # cursor plane. Costs some CPU vs GPU-accelerated compositing.
    exec weston --modules=systemd-notify.so --use-pixman --log=/tmp/weston.log
fi
