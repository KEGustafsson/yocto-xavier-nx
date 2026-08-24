#!/bin/sh
# Put the boat computer into SC7 - the Tegra deep-sleep state, reached
# through the kernel's ordinary suspend-to-RAM path - and leave it wakeable
# with a Wake-on-LAN magic packet.
#
#   ssh root@boat boat-sleep            # the intended remote use
#   boat-sleep --status                 # report readiness, suspend nothing
#   boat-sleep --dry-run                # arm + check, stop before suspending
#   boat-sleep --force                  # suspend even if WoL is not armed
#   boat-sleep --delay 10               # seconds before logind is asked
#
# Why this is safe to run over SSH: `systemctl suspend` hands the request to
# logind and returns immediately, so the command's output and exit status
# reach the caller before the network goes away. The link then drops when
# the board actually suspends - an expected, clean-looking SSH disconnect.
#
# What actually enters SC7: Linux suspend-to-RAM on this BSP. /sys/power/
# mem_sleep must be set to "deep"; the "s2idle" alternative is a shallow
# idle loop that does NOT enter SC7 and saves comparatively little power,
# so this script insists on deep unless --force is given.
set -eu

CONF=/etc/default/boat-power
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_WOL_INTERFACES:=eth0}"
: "${BOAT_SLEEP_REQUIRE_WOL:=1}"
: "${BOAT_SLEEP_DELAY:=3}"

FORCE=0
DRY_RUN=0
STATUS_ONLY=0

say()  { echo "boat-sleep: $*"; }
die()  { echo "boat-sleep: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: boat-sleep [options]

Suspend the boat computer to SC7 (Tegra deep sleep), leaving it wakeable
with a Wake-on-LAN magic packet. Run it over SSH as root.

  -s, --status     report SC7/Wake-on-LAN readiness and exit, changing nothing
  -n, --dry-run    arm Wake-on-LAN and run every check, but do not suspend
  -f, --force      suspend anyway: without Wake-on-LAN armed, into a
                   non-deep sleep state, and past any inhibitor lock
  -d, --delay N    seconds to wait before asking logind to suspend
  -h, --help       this text

Defaults come from /etc/default/boat-power.
USAGE
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -f|--force)   FORCE=1 ;;
        -n|--dry-run) DRY_RUN=1 ;;
        -s|--status)  STATUS_ONLY=1 ;;
        -d|--delay)   [ "$#" -ge 2 ] || die "--delay needs a value"
                      BOAT_SLEEP_DELAY="$2"; shift ;;
        -h|--help)    usage 0 ;;
        *)            echo "boat-sleep: unknown argument '$1'" >&2; usage 1 ;;
    esac
    shift
done

case "$BOAT_SLEEP_DELAY" in
    ''|*[!0-9]*) die "the suspend delay must be a whole number of seconds (--delay, or BOAT_SLEEP_DELAY in $CONF), got '$BOAT_SLEEP_DELAY'" ;;
esac

[ "$(id -u)" = "0" ] || die "must run as root (arming WoL and selecting the sleep state both write to /sys) - try 'ssh root@<boat>'"

# --- Is suspend-to-RAM there at all? ---------------------------------------
[ -w /sys/power/state ] || die "/sys/power/state is missing - this kernel has no suspend support (CONFIG_SUSPEND)"
grep -qw mem /sys/power/state || die "'mem' is not in /sys/power/state ($(cat /sys/power/state)) - suspend-to-RAM is not available on this kernel"

# --- Deep (SC7) rather than s2idle -----------------------------------------
# /sys/power/mem_sleep looks like "s2idle [deep]"; the brackets mark the
# state that "mem" currently means. No such file on a kernel too old for the
# multi-state interface - there "mem" is the platform state by definition.
sleep_state="deep (assumed - no /sys/power/mem_sleep on this kernel)"
if [ -e /sys/power/mem_sleep ]; then
    modes=$(cat /sys/power/mem_sleep)
    case "$modes" in
        *"[deep]"*) ;;
        *deep*)
            say "selecting deep sleep (was: $modes)"
            echo deep > /sys/power/mem_sleep \
                || die "could not select 'deep' in /sys/power/mem_sleep" ;;
        *)
            if [ "$FORCE" = "1" ]; then
                say "WARNING: no 'deep' state offered (/sys/power/mem_sleep: $modes) - --force given, suspending to '$modes' anyway; this does NOT enter SC7"
            else
                die "no 'deep' state offered (/sys/power/mem_sleep: $modes) - this kernel/BSP would suspend to s2idle, which is not SC7. Re-run with --force to accept that."
            fi ;;
    esac
    sleep_state=$(cat /sys/power/mem_sleep)
fi

# --- Arm Wake-on-LAN before, not after, deciding to suspend ----------------
# --status must not change anything, so it only reads the flags; every other
# mode arms first. boat-wol-arm's own exit status is not what decides here -
# the interfaces' read-back state is, so that "armed" always means "the NIC
# says magic-packet wake is on right now", however it got that way (this
# call, boat-wol.service at boot, or NetworkManager re-applying it). Its
# messages are still what explain a failure to arm.
if [ "$STATUS_ONLY" != "1" ]; then
    /usr/bin/boat-wol-arm || true
fi

wol_ok=0
macs=""
for ifc in $BOAT_WOL_INTERFACES; do
    [ -r "/sys/class/net/$ifc/address" ] || continue
    case "$(ethtool "$ifc" 2>/dev/null | sed -n 's/^[[:space:]]*Wake-on:[[:space:]]*//p')" in
        *g*) wol_ok=1
             macs="$macs $(cat "/sys/class/net/$ifc/address")" ;;
    esac
done
macs="${macs# }"

if [ "$STATUS_ONLY" = "1" ]; then
    say "sleep state:  $sleep_state"
    if [ "$wol_ok" = "1" ]; then
        say "wake-on-lan:  armed on ${macs:-(unknown MAC)}"
        say "wake with:    wakeonlan ${macs%% *}   (or scripts/wake-boat.sh in the yocto-xavier-nx checkout)"
    else
        say "wake-on-lan:  NOT armed - 'boat-wol-arm' says why; 'systemctl status boat-wol' has the boot-time attempt"
    fi
    say "suspend now:  boat-sleep"
    exit 0
fi

if [ "$wol_ok" != "1" ]; then
    if [ "$FORCE" != "1" ] && [ "$BOAT_SLEEP_REQUIRE_WOL" = "1" ]; then
        die "no interface is armed for magic-packet wake - refusing to suspend, because nothing on the network could wake this board again. Fix it (see the boat-wol messages above), or accept it with 'boat-sleep --force', or set BOAT_SLEEP_REQUIRE_WOL=0 in $CONF."
    fi
    say "WARNING: suspending WITHOUT Wake-on-LAN - only a local button press or a power cycle will bring this board back"
fi

# --- Go ---------------------------------------------------------------------
if [ -n "$macs" ]; then
    say "wake it with: wakeonlan ${macs%% *}"
fi

if [ "$DRY_RUN" = "1" ]; then
    say "--dry-run: checks done, not suspending"
    exit 0
fi

say "entering SC7 (${sleep_state}) in ${BOAT_SLEEP_DELAY}s"
say "the SSH connection will drop when the board suspends - that is the sleep, not a failure"

command -v systemctl >/dev/null 2>&1 || die "systemctl not found"

# Let logind run the suspend rather than writing 'mem' to /sys/power/state
# directly: that way inhibitors are honoured and the systemd system-sleep
# hooks (which re-arm WoL on resume, and optionally park the watchdog) run
# on both sides of it.
if [ "$BOAT_SLEEP_DELAY" -gt 0 ]; then
    sleep "$BOAT_SLEEP_DELAY"
fi

# An inhibitor lock - the XFCE power manager holding one on the helm
# display, say - makes logind refuse, and `systemctl suspend` says so and
# exits non-zero, which is what the SSH caller sees. `systemd-inhibit
# --list` names the holder. --force takes the request past them.
if [ "$FORCE" = "1" ]; then
    exec systemctl suspend -i
fi
exec systemctl suspend
