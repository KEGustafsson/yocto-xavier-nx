#!/bin/sh
# Arm Wake-on-LAN (magic packet) on the boat computer's Ethernet
# interface(s) and mark the device as a wakeup source, so the box can be
# brought back out of SC7 deep sleep from anywhere on the boat's LAN.
#
#   boat-wol-arm            # arm the interfaces in /etc/default/boat-power
#   boat-wol-arm eth0 eth1  # or arm exactly these
#   boat-wol-arm --check    # report only, write nothing (what boat-sleep
#                           # --status uses, so it can promise that)
#
# "Armed" means BOTH halves of the wake path are in place: the MAC has the
# magic-packet flag (`Wake-on: g`), AND - where the device exposes one - its
# bus wake source (power/wakeup) is enabled. The NIC flag alone is not
# enough: with the device's wake source disabled the packet arrives at a NIC
# whose wake signal goes nowhere, and the board sleeps through it. Both are
# checked by reading the state back, never by trusting a write's exit code.
#
# Run at boot by boat-wol.service, and again from the systemd system-sleep
# hook on both sides of a suspend: several drivers drop the WoL flag when
# the link goes down or the device is re-probed on resume, so arming it once
# at boot is not enough to rely on.
#
# Exit status is deliberate: 1 when NOTHING is armed, which is what makes
# `systemctl status boat-wol` red on a kernel/driver/PHY combination that
# cannot do magic-packet wake at all, and what boat-sleep's refuse-to-sleep
# interlock reads. That is the single most useful thing to know before
# trusting a remote `boat-sleep`, so it is reported as a failure rather than
# logged and swallowed.
set -eu

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_WOL_INTERFACES:=eth0}"

say() { echo "boat-wol: $*"; }

CHECK_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        -c|--check) CHECK_ONLY=1 ;;
        --)         shift; break ;;
        -*)         say "unknown option '$1' (usage: boat-wol-arm [--check] [interface ...])"
                    exit 2 ;;
        *)          break ;;
    esac
    shift
done

# Explicit interfaces on the command line win over the config file.
if [ "$#" -gt 0 ]; then
    BOAT_WOL_INTERFACES="$*"
fi

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

    # Separate from the MAC's own WoL flag: the bus glue (PCIe, USB, the
    # Tegra device) has to be allowed to wake the system too. Not every
    # device exposes this attribute - a missing one is normal, and then the
    # NIC flag is all there is to check.
    wakeup="/sys/class/net/$ifc/device/power/wakeup"

    if [ "$CHECK_ONLY" = "0" ]; then
        if ! ethtool -s "$ifc" wol g 2>/dev/null; then
            say "$ifc: 'ethtool -s $ifc wol g' failed (run as root?)"
            continue
        fi
        # 2>/dev/null BEFORE the output redirect, deliberately: redirections
        # are applied left to right, and a refused open is reported by the
        # SHELL - i.e. before a trailing 2>/dev/null would be in effect. The
        # readback below is what actually decides; this keeps the duplicate
        # complaint out of the journal.
        if [ -f "$wakeup" ] && ! echo enabled 2>/dev/null > "$wakeup"; then
            say "$ifc: could not write $wakeup (kernel refused)"
        fi
    fi

    # Read both halves back rather than trusting the writes: ethtool exits 0
    # on drivers that accept `wol g` and then quietly keep it disabled, and
    # the wakeup write can be refused just as quietly.
    now=$(wol_current "$ifc")
    case "$now" in
        *g*) ;;
        *)   say "$ifc: Wake-on is '${now:-unknown}', not 'g' - not armed"
             continue ;;
    esac

    if [ -f "$wakeup" ]; then
        wakeup_state=$(cat "$wakeup" 2>/dev/null || echo unknown)
        if [ "$wakeup_state" != "enabled" ]; then
            say "$ifc: NIC has the magic-packet flag, but its wake source is '$wakeup_state' ($wakeup) - the packet would reach a NIC whose wake signal goes nowhere, so this does NOT count as armed"
            continue
        fi
    fi

    say "$ifc: armed for magic-packet wake (Wake-on: $now, MAC $(cat "/sys/class/net/$ifc/address"))"
    armed=$((armed + 1))
done

if [ "$armed" -eq 0 ]; then
    say "no interface armed out of $tried checked - this system will NOT wake from SC7 over Ethernet"
    exit 1
fi

exit 0
