#!/bin/sh
# boat-fault - BENCH IMAGES ONLY (boat-image-faulty, BOAT_BENCH=1).
#
# Makes a TRIAL boot of the slot it is installed in fail in one chosen way,
# so the bench can prove the A/B fallback catches it
# (docs/plans/ab-os-updates.md section 7, tests 13-18; docs/plans/
# ab-validation.md). The way is fixed at build time, BOAT_FAULT:
#
#   panic      a kernel panic                          (test 13: panic=10)
#   emergency  systemd's emergency mode                (test 15: 60 s reboot)
#   tier2      NetworkManager down: the health gate    (test 16: mark-bad)
#              fails its Tier 2 check
#   hang       boat-commit never finishes              (test 18: deadline)
#
# Only ever on a trial boot (boat.trial=1): a slot that has fallen back is
# marked bad and is not booted again, and nothing here can affect a
# committed slot.
set -eu
kind="@FAULT@"

say() {
    echo "boat-fault: $*"
    echo "boat-fault: $*" > /dev/kmsg 2>/dev/null || true
}

case "$kind" in
    panic)
        say "BENCH FAULT: kernel panic now"
        sync
        echo 1 > /proc/sys/kernel/sysrq
        echo c > /proc/sysrq-trigger
        ;;
    emergency)
        say "BENCH FAULT: dropping to emergency mode"
        systemctl --no-block emergency
        ;;
    tier2)
        say "BENCH FAULT: masking NetworkManager so the health gate fails"
        systemctl mask --runtime --now NetworkManager.service
        ;;
    hang)
        say "BENCH FAULT: holding boat-commit so only the trial deadline can end this boot"
        mkdir -p /run/systemd/system/boat-commit.service.d
        printf '[Service]\nExecStartPre=/bin/sleep infinity\nTimeoutStartSec=infinity\n' \
            > /run/systemd/system/boat-commit.service.d/10-boat-fault.conf
        systemctl daemon-reload
        ;;
    *)
        say "unknown BOAT_FAULT '$kind' - doing nothing"
        exit 1
        ;;
esac
