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

# Snapshot BOAT_WOL_WAIT before sourcing the config, and put it back after:
# the file is plain `VAR=value` assignments, so it would otherwise overwrite a
# value passed in the environment - and the systemd-sleep hook passes
# BOAT_WOL_WAIT=0 on resume precisely to avoid a per-interface stall while the
# sleep operation is still open. An operator who sets BOAT_WOL_WAIT in this
# CONFFILE (which its own comments invite) would silently get that stall back.
# Same "environment beats the file" rule load_boat_conf applies on the host.
_env_wol_wait="${BOAT_WOL_WAIT-}"
_env_wol_wait_set="${BOAT_WOL_WAIT+set}"

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
[ "$_env_wol_wait_set" = "set" ] && BOAT_WOL_WAIT="$_env_wol_wait"
unset _env_wol_wait _env_wol_wait_set

: "${BOAT_WOL_INTERFACES:=eth0}"
# Seconds to wait for a driver to advertise magic-packet support before
# giving up on an interface. Only used when actually arming.
: "${BOAT_WOL_WAIT:=15}"
# /etc/default/boat-power is a CONFFILE, meant to be hand-edited on the boat,
# so this can arrive as anything. It has to be validated: `[ "$waited" -ge 15s ]`
# is an error, not a false, and the `&&` list it sits in swallows that under
# `set -e` - so the break never fires and the wait loop below spins forever at
# one second per turn. That hangs boat-wol.service to its systemd start
# timeout, hangs NetworkManager's dispatcher queue, and hangs the pre-suspend
# hook mid-suspend.
case "$BOAT_WOL_WAIT" in
    ''|*[!0-9]*)
        echo "boat-wol: BOAT_WOL_WAIT must be a whole number of seconds, got '$BOAT_WOL_WAIT'" >&2
        echo "boat-wol: fix it in $CONF" >&2
        exit 2 ;;
esac

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

    # Wait for the capability to appear, don't just sample it once.
    # CONFIRMED ON HARDWARE (Xavier NX devkit, nvethernet): this driver
    # reports "Supports Wake-on: d" - i.e. nothing - until the PHY is
    # attached at link-up, and only then advertises 'g'. On a real boot the
    # gap was 4.4 seconds:
    #
    #   [12.178] boat-wol-arm: supports Wake-on 'd' ... skipped   <- unit failed
    #   [16.541] nvethernet eth0: Link is Up - 1Gbps/Full
    #
    # boat-wol.service now orders itself after network-online.target, which
    # closes that gap on a normal boot; this loop is the backstop for the
    # cases ordering cannot cover - a cable plugged in late, a switch slow to
    # negotiate, or the driver advertising capability slightly after carrier.
    # Skipped entirely in --check mode: a readiness report should answer
    # about the state right now, not block for ten seconds first.
    caps=$(wol_supported "$ifc")
    if [ "$CHECK_ONLY" = "0" ]; then
        waited=0
        while [ -z "$caps" ] || [ "${caps#*g}" = "$caps" ]; do
            [ "$waited" -ge "$BOAT_WOL_WAIT" ] && break
            sleep 1
            waited=$((waited + 1))
            caps=$(wol_supported "$ifc")
        done
        # Only when the wait actually achieved something. The old condition
        # was satisfied by a TIMEOUT with caps still "d", so the journal read
        # "waited 15s for the driver to advertise Wake-on-LAN" and then
        # "supports Wake-on 'd' ... skipped" - success followed by failure.
        case "$caps" in
            *g*) [ "$waited" -gt 0 ] \
                     && say "$ifc: waited ${waited}s for the driver to advertise Wake-on-LAN" ;;
        esac
    fi
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
