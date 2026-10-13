#!/bin/sh
# Validate the A/B boot state on the ESP, and rebuild it if it is torn.
#
# grub.cfg can only rewrite a grubenv block that is already valid, and RAUC
# rewrites the file with grub-editenv (truncate, then write) - not atomic on
# vfat, so a power cut at the wrong moment can leave it unreadable. GRUB then
# boots from grubenv.bak or its built-in defaults, but cannot count attempts
# until the file is whole again. This runs on every boot that reaches Linux:
#
#   valid grubenv -> keep grubenv.bak as a copy of it (last known good)
#   torn grubenv  -> rebuild it from what this boot itself proves:
#     - the slot that booted works, and stays on trial if it was booted as
#       one (boat.trial=1), so its health gate still decides about it;
#     - on a trial boot, the other slot is the committed one the update was
#       installed from (no install starts during a trial), so it stays the
#       fallback;
#     - on a committed boot, the other slot may be half way through an
#       install - the torn write may have been RAUC's own - so it is marked
#       unusable until the next update writes it. grubenv.bak is never
#       trusted for it: it predates whatever happened since this boot began.
#
# A rebuild is reported in /run/boat-ab/grubenv-repaired and in the journal.
# Nothing is touched when the board came up outside GRUB, or when the disk
# does not look like the layout this image was built for.
#
# See docs/plans/ab-os-updates.md section 5.3 ("grubenv integrity").
set -eu
SELF=boat-grubenv-guard
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

mkdir -p "$BOAT_AB_RUNDIR"
mountpoint -q "$BOAT_AB_ESP_MOUNT" || die "the ESP is not mounted at $BOAT_AB_ESP_MOUNT"
check_layout || die "the disk does not match this image's A/B layout - not touching grubenv"

if grubenv_valid "$BOAT_AB_GRUBENV"; then
    grubenv_sync_bak
    exit 0
fi

slot="$(booted_slot)"
[ -n "$slot" ] || die "grubenv is invalid and this boot came from outside GRUB - cannot tell which slot is good"
other="$(other_slot "$slot")"

warn "grubenv is unreadable or inconsistent - rebuilding it"
if booted_on_trial; then
    # GRUB counted this attempt before the kernel loaded.
    slot_env="${slot}_OK=1 ${slot}_TRY=1 ${slot}_COMMITTED=0"
    other_env="${other}_OK=1 ${other}_TRY=0 ${other}_COMMITTED=1"
    note="$slot first and still on trial, $other its committed fallback"
else
    slot_env="${slot}_OK=1 ${slot}_TRY=0 ${slot}_COMMITTED=1"
    other_env="${other}_OK=0 ${other}_TRY=0 ${other}_COMMITTED=0"
    note="$slot first and committed, $other unusable until an update rewrites it"
fi

tmp="${BOAT_AB_GRUBENV}.rebuild"
rm -f "$tmp"
grub-editenv "$tmp" create
# shellcheck disable=SC2086 # the *_env strings are KEY=VALUE words
grub-editenv "$tmp" set ORDER="$slot $other" $slot_env $other_env BOAT_STAY=0
grubenv_valid "$tmp" || die "rebuilt grubenv does not validate - leaving the torn one"
replace_file "$tmp" "$BOAT_AB_GRUBENV"
grubenv_sync_bak
date -u +%Y-%m-%dT%H:%M:%SZ > "$BOAT_AB_RUNDIR/grubenv-repaired"
say "grubenv rebuilt: $note"
