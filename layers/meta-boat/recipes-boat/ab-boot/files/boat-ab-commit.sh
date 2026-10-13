#!/bin/sh
# Confirm that the slot GRUB booted came up: reset its attempt count and mark
# it committed in grubenv.
#
# PHASE 1 STUB (docs/plans/ab-os-updates.md section 6): this commits any
# boot that reaches multi-user.target, with no health gate. Phase 4 replaces
# it with boat-commit, which only commits a slot on trial once the Tier 2
# health checks pass and resets the board when they fail.
#
# Without this, every boot of a slot would count as an unconfirmed attempt
# and GRUB would mark even a working slot unusable on its next boot.
set -eu
SELF=boat-ab-commit
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

slot="$(booted_slot)"
[ -n "$slot" ] || { say "booted outside GRUB - nothing to confirm"; exit 0; }
mountpoint -q "$BOAT_AB_ESP_MOUNT" || die "the ESP is not mounted at $BOAT_AB_ESP_MOUNT"
grubenv_valid "$BOAT_AB_GRUBENV" || die "grubenv is not valid - boat-grubenv-guard should have rebuilt it"

grubenv_set "${slot}_OK=1" "${slot}_TRY=0" "${slot}_COMMITTED=1"
say "slot $slot confirmed: attempts reset, committed"
