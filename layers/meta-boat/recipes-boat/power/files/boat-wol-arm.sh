#!/bin/sh
# Arm Wake-on-LAN (magic packet) on the boat computer's Ethernet
# interface(s) and mark the device as a wakeup source, so the box can be
# brought back out of SC7 deep sleep from anywhere on the boat's LAN.
#
#   boat-wol-arm            # arm the interfaces in /etc/default/boat-power
#   boat-wol-arm eth0 eth1  # or arm exactly these
#
# Run at boot by boat-wol.service, and again from the systemd system-sleep
# hook on both sides of a suspend: several drivers drop the WoL flag when
# the link goes down or the device is re-probed on resume, so arming it once
# at boot is not enough to rely on.
#
# Exit status is deliberate: 1 when NOTHING could be armed, which is what
# makes `systemctl status boat-wol` red on a kernel/driver/PHY combination
# that cannot do magic-packet wake at all. That is the single most useful
# thing to know before trusting a remote `boat-sleep`, so it is reported as
# a failure rather than logged and swallowed.
set -eu

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_WOL_INTERFACES:=eth0}"

# Explicit interfaces on the command line win over the config file.
if [ "$#" -gt 0 ]; then
    BOAT_WOL_INTERFACES="$*"
fi

say() { echo "boat-wol: $*"; }

command -v ethtool >/dev/null 2>&1 || {
    say "ethtool not installed - cannot arm Wake-on-LAN"
    exit 1
}

# `ethtool eth0` prints both of these, and they are NOT the same thing:
#
#     Supports Wake-on: pumbg     <- what the driver can do
#             Wake-on: d          <- what is set right now ('d' = disabled)
#
# Anchoring the second pattern on start-of-line-plus-whitespace is what keeps
# it from also matching the "Supports Wake-on:" line, which would make an
# unarmable interface look armed.
wol_supported() { ethtool "$1" 2>/dev/null | sed -n 's/^[[:space:]]*Supports Wake-on:[[:space:]]*//p'; }
wol_current()   { ethtool "$1" 2>/dev/null | sed -n 's/^[[:space:]]*Wake-on:[[:space:]]*//p'; }
wol_driver()    { ethtool -i "$1" 2>/dev/null | sed -n 's/^driver:[[:space:]]*//p'; }

armed=0
tried=0

for ifc in $BOAT_WOL_INTERFACES; do
    tried=$((tried + 1))

    if [ ! -e "/sys/class/net/$ifc" ]; then
        say "$ifc: no such interface - skipped"
        continue
    fi

    caps=$(wol_supported "$ifc")
    case "$caps" in
        *g*) ;;
        "")  say "$ifc: driver '$(wol_driver "$ifc")' reports no Wake-on-LAN support - skipped"
             continue ;;
        *)   say "$ifc: driver '$(wol_driver "$ifc")' supports Wake-on '$caps' but not magic packet ('g') - skipped"
             continue ;;
    esac

    if ! ethtool -s "$ifc" wol g 2>/dev/null; then
        say "$ifc: 'ethtool -s $ifc wol g' failed (run as root?)"
        continue
    fi

    # Separate from the MAC's own WoL flag: the bus glue (PCIe, USB, the
    # Tegra device) has to be allowed to wake the system too, or the packet
    # arrives at a NIC whose wake signal goes nowhere. Not every device has
    # this attribute - a missing one is normal, not an error.
    wakeup="/sys/class/net/$ifc/device/power/wakeup"
    if [ -f "$wakeup" ]; then
        if ! echo enabled > "$wakeup" 2>/dev/null; then
            say "$ifc: could not enable $wakeup (kernel refused) - wake may not work"
        fi
    fi

    # Read it back rather than trusting the write: ethtool exits 0 on
    # drivers that accept `wol g` and then quietly keep it disabled.
    now=$(wol_current "$ifc")
    case "$now" in
        *g*) say "$ifc: armed for magic-packet wake (Wake-on: $now, MAC $(cat "/sys/class/net/$ifc/address"))"
             armed=$((armed + 1)) ;;
        *)   say "$ifc: setting did not stick (Wake-on: ${now:-unknown}) - driver accepted it but did not apply it" ;;
    esac
done

if [ "$armed" -eq 0 ]; then
    say "no interface armed out of $tried tried - this system will NOT wake from SC7 over Ethernet"
    exit 1
fi

exit 0
