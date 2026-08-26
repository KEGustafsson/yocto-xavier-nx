#!/bin/sh
# systemd system-sleep hook - installed as system-sleep/boat-power, run by
# systemd-sleep as:  boat-power pre|post  suspend|hibernate|hybrid-sleep
#
# It exists so that WHATEVER triggers the sleep - boat-sleep over SSH, a
# plain `systemctl suspend`, an XFCE power-manager menu item on the helm
# display - the board comes back the same way:
#
#   pre   arm Wake-on-LAN one last time before the network goes down,
#         optionally park the watchdog (see BOAT_SLEEP_STOP_WATCHDOG)
#   post  re-arm Wake-on-LAN, because a driver that re-probes the MAC on
#         resume comes back with wol=d, and restart anything parked above
#
# Output goes to the journal (systemd-sleep captures it); the exit status is
# ignored by systemd, so nothing here can block or fail a suspend.
set -eu

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_SLEEP_STOP_WATCHDOG:=0}"

# Marker so "post" only restarts a watchdog that "pre" actually stopped -
# never one the operator had deliberately left down. /run is tmpfs, but the
# whole point is that it survives a suspend/resume, which it does.
STAMP=/run/boat-power.watchdog-parked

case "${1:-}" in
    pre)
        echo "boat-power: preparing for ${2:-suspend} (SC7)"
        if ! /usr/bin/boat-wol-arm; then
            echo "boat-power: Wake-on-LAN is NOT armed - nothing on the network will wake this board"
        fi
        if [ "$BOAT_SLEEP_STOP_WATCHDOG" = "1" ] \
           && systemctl is-active --quiet watchdog.service; then
            echo "boat-power: stopping watchdog.service across the sleep"
            if systemctl stop watchdog.service; then
                : > "$STAMP"
            fi
        fi
        ;;
    post)
        # BOAT_WOL_WAIT=0: on resume the MAC has just re-probed, so it reports
        # no Wake-on-LAN support for a moment and boat-wol-arm's default
        # 15-second wait would be spent in full, PER INTERFACE, while
        # systemd-sleep still holds the sleep inhibitor and `systemctl suspend`
        # has not returned. NetworkManager's dispatcher (90-boat-wol) does the
        # authoritative re-arm a moment later once the link is actually up -
        # this call is the belt to its braces, not the mechanism.
        if ! BOAT_WOL_WAIT=0 /usr/bin/boat-wol-arm; then
            echo "boat-power: Wake-on-LAN could not be re-armed after resume"
        fi
        if [ -e "$STAMP" ]; then
            rm -f "$STAMP"
            echo "boat-power: restarting watchdog.service after resume"
            systemctl start watchdog.service || true
        fi
        echo "boat-power: resumed from ${2:-suspend}"
        ;;
esac

exit 0
