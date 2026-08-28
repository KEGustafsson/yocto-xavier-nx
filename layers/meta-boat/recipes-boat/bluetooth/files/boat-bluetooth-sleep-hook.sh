#!/bin/sh
# systemd system-sleep hook - installed as system-sleep/boat-bluetooth, run
# by systemd-sleep as:  boat-bluetooth pre|post  suspend|hibernate|hybrid-sleep
#
# It exists so that WHATEVER triggers the sleep - boat-sleep over SSH, a
# plain `systemctl suspend`, an XFCE power-manager menu item on the helm
# display - the Bluetooth radio comes back the way it went down:
#
#   pre   record whether Bluetooth was powered
#   post  hand the restore to boat-bluetooth-resume.service and get out of
#         the way
#
# WHY "post" DELEGATES INSTEAD OF DOING THE WORK
# Restoring has to wait for the devkit's RTL8822CE Bluetooth function to
# re-enumerate on the USB bus and for bluetoothd to probe the new index -
# seconds, not milliseconds. systemd-sleep runs these hooks synchronously and
# holds the sleep operation open (inhibitor still taken) until the last one
# returns, so waiting HERE would stall the tail of every resume. boat-power's
# own hook dodges the same trap by passing BOAT_WOL_WAIT=0 and leaving the
# authoritative re-arm to NetworkManager's dispatcher; this one starts a
# service with --no-block and returns immediately, which is the same trick.
#
# Output goes to the journal (systemd-sleep captures it); the exit status is
# ignored by systemd, so nothing here can block or fail a suspend.
set -eu

case "${1:-}" in
    pre)
        /usr/bin/boat-bt-power save || echo "boat-bluetooth: could not record the Bluetooth state before ${2:-suspend}"
        ;;
    post)
        # --no-block: return as soon as systemd has queued the job. Without
        # it this waits for the whole restore, which is the stall described
        # above. Failures show up in `systemctl status
        # boat-bluetooth-resume`, not here.
        systemctl --no-block start boat-bluetooth-resume.service \
            || echo "boat-bluetooth: could not start boat-bluetooth-resume.service - Bluetooth may stay off after this resume"
        ;;
esac

exit 0
