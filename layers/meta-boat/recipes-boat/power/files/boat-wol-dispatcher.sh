#!/bin/sh
# NetworkManager dispatcher script, installed as
# /etc/NetworkManager/dispatcher.d/90-boat-wol. NM runs it as
# "<interface> <action>" after it has finished acting on a connection.
#
# WHY THIS EXISTS, when 90-boat-wol.conf already sets NM's own
# ethernet.wake-on-lan default: because that default does not take effect.
# CONFIRMED ON HARDWARE - nmcli reports the active profile's wake-on-lan as
# "default", and the magic-packet flag is clear both at boot and after every
# resume, no matter what boat-wol-arm did beforehand.
#
# The mechanism, measured on a real suspend/resume cycle:
#
#   [655.613844] hook: boat-wol: eth0: armed for magic-packet wake (Wake-on: g)
#   [655.655000] NM: sleep: wake requested
#   [655.662906] NM: device (eth0): activated -> unmanaged (reason 'sleeping')
#   [655.794007] NM: device (eth0): unmanaged -> unavailable (reason 'managed')
#
# NM tore the interface down 41 MILLISECONDS after the systemd-sleep hook
# armed it, then brought it back up applying the profile's own (unset,
# therefore driver-default) wake-on-lan - clearing the flag. Anything that
# arms WoL *before* NM finishes is racing a fight it will lose.
#
# A dispatcher script cannot lose that race: NM calls it after the activation
# it would otherwise clobber. That covers all three paths at once - boot,
# resume from SC7, and a cable replugged mid-voyage - which is why this is
# the durable fix and boat-wol.service is now only the early-boot best
# effort.
#
# Dispatcher scripts must be owned by root and not writable by group or
# other, or NM silently refuses to run them. The recipe installs this 0755
# root:root.
set -eu

IFACE="${1:-}"
ACTION="${2:-}"

# "up" is the activation that clears the flag. "dhcp4-change" covers a lease
# renewal that re-applies device config. Everything else - down, pre-up,
# connectivity-change, the vpn-* actions - either has no flag to restore or
# would fire so often it would just add noise.
case "$ACTION" in
    up|dhcp4-change) ;;
    *) exit 0 ;;
esac

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_WOL_INTERFACES:=eth0}"

# Only touch the interfaces the operator actually nominated. NM runs this for
# every device it manages - wlan0, docker0, any USB adapter - and arming
# something that was never asked for would be surprising.
for want in $BOAT_WOL_INTERFACES; do
    if [ "$want" = "$IFACE" ]; then
        # Short wait, not boat-wol-arm's default: NM only calls this once the
        # interface is up, so the driver has already attached its PHY and is
        # advertising the capability. A long wait here would hold up NM's
        # dispatcher queue for nothing.
        BOAT_WOL_WAIT=3 exec boat-wol-arm "$IFACE"
    fi
done

exit 0
