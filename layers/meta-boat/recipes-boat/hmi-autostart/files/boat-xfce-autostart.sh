# shellcheck shell=sh
# No shebang: this is sourced by the login shell as /etc/profile.d/*.sh,
# not executed. The directive above is what tells a static check which
# dialect to hold it to.
#
# Installed as /etc/profile.d/boat-xfce-autostart.sh.
#
# Starts the helm display: an Xorg server plus the XFCE desktop, run as the
# console session's own user (BOAT_HMI_USER, put there by getty autologin),
# straight from that login shell.
#
# Deliberately NOT xserver-nodm-init (poky's display-manager-less X launcher):
# that unit runs Xorg as root, so the whole desktop - and every app it
# spawns - would run as root, and systemd-logind would never create the
# /run/user/<uid> session directory this image's design pins to
# BOAT_HMI_UID. Starting X from the autologin session instead keeps the
# desktop unprivileged and gives it a real logind seat, which is also what
# lets a non-root Xorg take DRM master and open input devices at all.
# xserver-nodm-init is therefore left out of packagegroup-boat-hmi entirely -
# it declares "Alias=display-manager.service" and would race this session for
# the same VT if it were ever installed.
#
# This replaces the Weston/Wayland session earlier versions of this image
# booted into. Containerized GUI apps talk to X directly now, so there is no
# XWayland bridge in the path any more - see
# docs/05-phase2-boat-computer-layer.md.

if [ "$(id -u)" = "@BOAT_HMI_UID@" ] && [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    # Xorg creates this itself, but pre-creating it keeps the bind-mount
    # target container GUI apps use predictable, and existing before any of
    # them start. Mode 1777 deliberately: it is what X's own transport layer
    # expects to find, and it is what lets a container running as some other
    # uid connect to the socket in it. /tmp is 1777 so the unprivileged
    # session user can create it, and can set the sticky bit on a directory
    # it owns.
    mkdir -p /tmp/.X11-unix
    chmod 1777 /tmp/.X11-unix 2>/dev/null || :

    # startx (from the "xinit" package) handles the MIT-MAGIC-COOKIE dance and
    # kills the server when the session client exits; the login shell then
    # ends, agetty respawns, autologin runs again - i.e. the desktop restarts
    # by itself if it ever dies.
    #
    #   :0    fixed display number - containers hardcode DISPLAY=:0.
    #   vt1   pin Xorg to the VT this logind session already owns. A non-root
    #         Xorg gets its DRM/input devices through logind's TakeDevice on
    #         that seat, so the VT must be the session's own, not a free one
    #         Xorg picked for itself.
    #
    # startx adds "-nolisten tcp" by default: X is reachable over the local
    # unix socket only, never the network.
    #
    # X server log: ~/.local/share/xorg/Xorg.0.log (Xorg redirects there by
    # itself when it isn't running as root - /var/log isn't writable here).
    # Session/desktop output goes next to it, in the session user's own
    # directory rather than the fixed /tmp path this used to use: /tmp is 1777,
    # so any local uid could pre-create that name, and the log records what the
    # helm session is doing.
    #
    # The mkdir is what makes the redirect safe to `exec` into. If the
    # redirection fails, `exec` fails, this non-interactive login shell exits,
    # agetty respawns it, autologin runs it again - a tight loop with the helm
    # display never coming up and nothing on screen to say why. Falling back to
    # /dev/null keeps a missing log from costing the desktop.
    BOAT_LOG_DIR="${HOME}/.local/share"
    mkdir -p "$BOAT_LOG_DIR" 2>/dev/null || :
    BOAT_LOG="${BOAT_LOG_DIR}/boat-xfce-session.log"
    : >> "$BOAT_LOG" 2>/dev/null || BOAT_LOG=/dev/null

    exec startx /usr/bin/boat-xfce-session -- :0 vt1 \
        >"$BOAT_LOG" 2>&1
fi
