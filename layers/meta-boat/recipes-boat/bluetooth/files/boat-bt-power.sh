#!/bin/sh
# Bluetooth adapter power state for the boat computer.
#
#   boat-bt-power status    # what every adapter is doing right now
#   boat-bt-power on        # power all adapters up   (same switch blueman flips)
#   boat-bt-power off       # power all adapters down
#   boat-bt-power save      # remember the current state   (pre-suspend)
#   boat-bt-power restore   # put the remembered state back (post-resume)
#
# WHAT "POWERED" MEANS HERE
# The org.bluez.Adapter1 Powered property - the same switch blueman's tray
# toggle and `bluetoothctl power on` flip, and the same one that reads as
# "off" in every UI. It is NOT the rfkill switch, but a soft rfkill block
# makes powering on fail, so the paths that power up clear one first.
#
# WHY D-BUS AND NOT hciconfig/btmgmt
# bluetoothd owns the controller through the kernel's mgmt socket. Going
# round it with hciconfig's raw ioctls (or a second mgmt client) can leave
# bluetoothd's idea of the adapter disagreeing with the hardware, which is
# how a controller ends up "up" but invisible to blueman. dbus-send talks to
# bluetoothd itself, so there is only ever one writer.
#
# BOOT IS NOT THIS SCRIPT'S JOB
# /etc/bluetooth/main.conf sets [Policy] AutoEnable=true, and bluetoothd
# powers each controller up as it probes it. This script exists for the one
# thing AutoEnable cannot express: carrying the state you actually had ACROSS
# a suspend, so an adapter you deliberately switched off does not come back
# on, and one you left on does. See boat-bluetooth-resume.service and the
# systemd system-sleep hook that starts it.
set -eu

CONF=/etc/default/boat-bluetooth
if [ -r "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
fi
: "${BOAT_BT_RESUME_RESTORE:=1}"
: "${BOAT_BT_RESUME_WAIT:=20}"
# What `restore` assumes when there is no saved state - a resume whose "pre"
# hook never ran, or a save that could not reach bluetoothd. "on" matches
# this image's AutoEnable=true policy: the default answer to "should the
# radio be on?" is yes.
: "${BOAT_BT_DEFAULT:=on}"

say() { echo "boat-bt: $*"; }

# A CONFFILE meant to be hand-edited on the boat, so validate rather than
# trust: a non-numeric wait would make `[ "$waited" -ge "$BOAT_BT_RESUME_WAIT" ]`
# an ERROR rather than a false, and the wait loop below would then never end -
# hanging boat-bluetooth-resume.service to its systemd start timeout.
case "$BOAT_BT_RESUME_WAIT" in
    ''|*[!0-9]*)
        say "BOAT_BT_RESUME_WAIT must be a whole number of seconds, got '$BOAT_BT_RESUME_WAIT' - fix it in $CONF" >&2
        exit 2 ;;
esac
case "$BOAT_BT_DEFAULT" in
    on|off) ;;
    *)  say "BOAT_BT_DEFAULT must be 'on' or 'off', got '$BOAT_BT_DEFAULT' - fix it in $CONF" >&2
        exit 2 ;;
esac

# /run is tmpfs, which is exactly right: the saved state has to survive the
# suspend (it does - RAM is kept in SC7) but must NOT survive a reboot, where
# AutoEnable is the policy instead.
STATE=/run/boat-bluetooth.powered

# Adapters that bluetoothd has actually PROBED, not merely ones the kernel
# has. Asking bluetoothd rather than listing /sys/class/bluetooth/hci* is
# what makes `restore` race-free after a resume: an adapter appears in
# GetManagedObjects only once policy_adapter_probe() has run for it, so by
# the time we can see it, AutoEnable has already had its say and our write is
# the last one. The trailing quote in the pattern is load-bearing - it is
# what keeps the device objects underneath an adapter (.../hci0/dev_XX_...)
# from matching as adapters themselves.
adapters() {
    dbus-send --system --print-reply --dest=org.bluez / \
        org.freedesktop.DBus.ObjectManager.GetManagedObjects 2>/dev/null \
    | sed -n 's|.*object path "\(/org/bluez/hci[0-9][0-9]*\)".*|\1|p' \
    | sort -u
}

# 0 = powered, 1 = not powered, 2 = could not tell (bluetoothd not running,
# no such object). The third case is a real answer and must stay distinct
# from the second: "I could not ask" must never be recorded as "it was off",
# or one failed save would switch the radio off on the next wake.
get_powered() {
    out=$(dbus-send --system --print-reply=literal --dest=org.bluez "$1" \
              org.freedesktop.DBus.Properties.Get \
              string:org.bluez.Adapter1 string:Powered 2>/dev/null) || return 2
    case "$out" in
        *true*)  return 0 ;;
        *false*) return 1 ;;
        *)       return 2 ;;
    esac
}

# get_powered's three states as a word. In the else branch $? is still the
# exit status of the `if` condition, which is what makes this readable at all.
powered_word() {
    if get_powered "$1"; then
        echo on
    else
        case "$?" in
            1) echo off ;;
            *) echo unknown ;;
        esac
    fi
}

set_powered() {
    dbus-send --system --print-reply --dest=org.bluez "$1" \
        org.freedesktop.DBus.Properties.Set \
        string:org.bluez.Adapter1 string:Powered "variant:boolean:$2" \
        >/dev/null 2>&1
}

# Clear a SOFT rfkill block, which is what a `Powered = true` would otherwise
# fail against. A HARD block is a physical switch and is deliberately left
# alone - rfkill will say so, which is more use than a silent failure.
unblock() {
    if command -v rfkill >/dev/null 2>&1; then
        rfkill unblock bluetooth || say "'rfkill unblock bluetooth' failed"
    fi
}

# "on" if ANY adapter is powered, "unknown" if there are none to ask. With
# the single on-board controller this image ships that is simply "is
# Bluetooth on"; with a USB dongle added as well it errs towards restoring a
# radio rather than silently leaving one down.
current_state() {
    found=0
    for a in $(adapters); do
        found=1
        if [ "$(powered_word "$a")" = on ]; then
            echo on
            return 0
        fi
    done
    if [ "$found" = 1 ]; then
        echo off
    else
        echo unknown
    fi
}

# Apply $1 (on|off) to every adapter, then READ IT BACK. dbus-send exits 0 on
# a Set that bluetoothd accepted and the controller then refused - an rfkill
# block that reappeared, firmware that did not load - so the write's exit
# status is not evidence. Returns 1 if any adapter did not end up as asked,
# or if there was no adapter to ask.
apply_state() {
    want="$1"
    case "$want" in
        on)  val=true ;;
        off) val=false ;;
        *)   say "internal error: apply_state '$want'"; return 2 ;;
    esac
    rc=0
    seen=0
    for a in $(adapters); do
        seen=1
        set_powered "$a" "$val" || true
        now=$(powered_word "$a")
        if [ "$now" = "$want" ]; then
            say "${a##*/}: powered $now"
        else
            say "${a##*/}: asked for '$want' but it is '$now' - 'rfkill list bluetooth' and 'journalctl -u bluetooth' say why"
            rc=1
        fi
    done
    if [ "$seen" = 0 ]; then
        say "no Bluetooth adapter is registered with bluetoothd (is bluetooth.service running?)"
        return 1
    fi
    return "$rc"
}

# Wait for bluetoothd to publish at least one adapter. After a resume the USB
# Bluetooth function has to re-enumerate, btusb has to bind and bluetoothd
# has to probe the new index - none of which has happened yet at the moment
# systemd-sleep runs its "post" hooks, which is precisely why the restore is
# handed to a service instead of being done in the hook.
wait_for_adapter() {
    waited=0
    while [ -z "$(adapters)" ]; do
        if [ "$waited" -ge "$BOAT_BT_RESUME_WAIT" ]; then
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    if [ "$waited" -gt 0 ]; then
        say "waited ${waited}s for bluetoothd to find the controller"
    fi
    return 0
}

command -v dbus-send >/dev/null 2>&1 || {
    say "dbus-send is not installed - cannot talk to bluetoothd"
    exit 1
}

case "${1:-status}" in
    status)
        found=0
        for a in $(adapters); do
            found=1
            say "${a##*/}: powered $(powered_word "$a")"
        done
        if [ "$found" = 0 ]; then
            say "no Bluetooth adapter is registered with bluetoothd (is bluetooth.service running?)"
        fi
        if command -v rfkill >/dev/null 2>&1; then
            rfkill list bluetooth 2>/dev/null | sed 's/^/boat-bt: rfkill: /'
        fi
        if [ -r "$STATE" ]; then
            say "state saved for the next resume: $(cat "$STATE")"
        fi
        exit 0
        ;;

    on|off)
        [ "$1" = on ] && unblock
        if apply_state "$1"; then exit 0; else exit 1; fi
        ;;

    save)
        state=$(current_state)
        echo "$state" > "$STATE"
        say "remembered Bluetooth state across the sleep: $state"
        exit 0
        ;;

    restore)
        if [ "$BOAT_BT_RESUME_RESTORE" != "1" ]; then
            say "restore disabled (BOAT_BT_RESUME_RESTORE=$BOAT_BT_RESUME_RESTORE in $CONF)"
            exit 0
        fi
        want=$(cat "$STATE" 2>/dev/null || echo "")
        case "$want" in
            on|off) ;;
            *)  say "no usable saved state (${want:-none}) - falling back to BOAT_BT_DEFAULT=$BOAT_BT_DEFAULT"
                want="$BOAT_BT_DEFAULT" ;;
        esac
        # Consume it: a stale file must not be replayed onto the NEXT resume,
        # whose own "pre" hook may not have got to run.
        rm -f "$STATE"

        [ "$want" = on ] && unblock

        if ! wait_for_adapter; then
            say "no Bluetooth adapter appeared within ${BOAT_BT_RESUME_WAIT}s of the resume - nothing to restore to (wanted '$want')"
            exit 1
        fi
        if apply_state "$want"; then exit 0; else exit 1; fi
        ;;

    -h|--help|help)
        sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;

    *)
        say "unknown command '$1' (try: status, on, off, save, restore)"
        exit 2
        ;;
esac
