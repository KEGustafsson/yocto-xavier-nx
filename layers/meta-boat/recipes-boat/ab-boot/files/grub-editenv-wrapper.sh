#!/bin/sh
# grub-editenv, as RAUC runs it: rauc.service puts /usr/lib/boat-ab/bin
# first on its PATH (10-boat-ab-rauc.conf), and RAUC's grub backend runs
# grub-editenv by name.
#
# The real grub-editenv rewrites grubenv in place - truncate, then write -
# and on the ESP's FAT a power cut in the middle can leave a block whose
# signature is intact but whose second half is stale: a "valid" grubenv
# with every key twice, where GRUB reads the stale copy and never sees its
# own attempt count advance. So a "set" on the A/B grubenv goes through
# lib.sh's grubenv_replace instead - under the A/B lock, on a copy that then
# replaces the file in one rename, with grubenv.bak kept in step - like
# every other grubenv write on the board. Anything else (list, a set on
# another file - grubenv_replace's own copy among them) goes to the real one.
#
# RAUC is told of a failure only when grubenv itself was not written. Once
# it was, the change has happened and RAUC must go on as if it had: a .bak
# copy that failed is an alert, and boat-grubenv-guard copies it on the next
# boot (GRUB reads grubenv first).
#
# See docs/plans/ab-os-updates.md section 5.3 ("grubenv integrity").
SELF=boat-ab-grub-editenv
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

REAL="${BOAT_AB_REAL_EDITENV:-/usr/bin/grub-editenv}"

if [ "${2:-}" = set ] && [ -n "${1:-}" ] \
        && [ "$(readlink -f "$1")" = "$(readlink -f "$BOAT_AB_GRUBENV")" ]; then
    shift 2
    ab_lock || die "could not take the A/B lock - not writing $BOAT_AB_GRUBENV"
    grubenv_replace "$@" || die "could not write $BOAT_AB_GRUBENV"
    grubenv_sync_bak || alert "could not copy grubenv to grubenv.bak after RAUC's write - GRUB's fallback copy is out of date until the next boot (the ESP will not take the write)"
    exit 0
fi
exec "$REAL" "$@"
