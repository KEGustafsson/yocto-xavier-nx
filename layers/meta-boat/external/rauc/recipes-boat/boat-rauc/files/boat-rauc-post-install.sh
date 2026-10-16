#!/bin/sh
# RAUC system post-install handler for the boat computer's A/B layout
# (docs/plans/ab-os-updates.md 5.6, step 8). RAUC has written and activated
# the new slot by now; this only keeps records, and never fails the install:
#
#   - grubenv.bak, GRUB's fallback if grubenv is ever torn, becomes a copy of
#     the grubenv RAUC just activated;
#   - one line per install goes to /data/boat-update/history, with any
#     warning the pre-install handler left for it (RAUC shows a handler's
#     output to nobody when the install succeeds).
SELF=boat-rauc-post-install
# shellcheck source=SCRIPTDIR/../../../../../recipes-boat/ab-boot/files/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

if grubenv_valid "$BOAT_AB_GRUBENV"; then
    grubenv_sync_bak || warn "could not update grubenv.bak"
else
    warn "grubenv is not valid after the install - boat-grubenv-guard repairs it on the next boot"
fi

targets=""
for n in ${RAUC_TARGET_SLOTS:-}; do
    case "$n" in ''|*[!0-9]*) continue ;; esac
    eval "targets=\"\$targets \${RAUC_SLOT_BOOTNAME_$n:-?}\""
done
note=""
warning_file="$BOAT_AB_RUNDIR/install-warning"
if [ -r "$warning_file" ]; then
    note=" - WARNING: $(head -n 1 "$warning_file")"
    rm -f "$warning_file"
fi
if mkdir -p /data/boat-update 2>/dev/null; then
    printf '%s installed %s into slot(s)%s, from slot %s%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${RAUC_META_BOAT_RELEASE:-?}" "$targets" "$(booted_slot)" "$note" \
        >> /data/boat-update/history || warn "could not write /data/boat-update/history"
fi
say "installed ${RAUC_META_BOAT_RELEASE:-?} into slot(s)$targets - it boots as a trial on the next reboot"
exit 0
