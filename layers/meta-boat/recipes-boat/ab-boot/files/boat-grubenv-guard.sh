#!/bin/sh
# Validate the A/B boot state on the ESP, and rebuild it if it is torn.
#
# grub.cfg can only rewrite a grubenv block that is already valid, and RAUC
# rewrites the file with grub-editenv (truncate, then write) - not atomic on
# vfat, so a power cut at the wrong moment can leave it unreadable. GRUB then
# boots from grubenv.bak or its built-in defaults, but cannot count attempts
# until the file is whole again. This runs on every boot that reaches Linux:
#
#   valid grubenv -> on a committed boot, give the slot back its attempts
#                    (TRY=0): it did boot, and doing it here - as soon as
#                    the ESP is mounted, not minutes later in boat-commit -
#                    keeps a run of power cuts during start-up from using up
#                    a good slot; then keep grubenv.bak as a copy of
#                    grubenv (last known good)
#   torn grubenv  -> rebuild it from what this boot itself proves:
#     - the slot that booted works, and stays on trial if it was booted as
#       one (boat.trial=1), so its health gate still decides about it;
#     - booted as GRUB's last resort (boat.lastresort=1), it may never have
#       passed a trial - GRUB passes the same flags either way - so it is
#       put on trial for the next boot rather than made committed, and the
#       other slot (which GRUB could not use either) is marked unusable;
#     - on a trial boot, the other slot is the committed one the update was
#       installed from (no install starts during a trial), so it stays the
#       fallback;
#     - on a committed boot, the other slot may be half way through an
#       install - the torn write may have been RAUC's own - so it is marked
#       unusable until the next update writes it. grubenv.bak is never
#       trusted for it: it predates whatever happened since this boot began.
#
# A rebuild is reported in /run/boat-ab/grubenv-repaired and in the journal;
# a failure, as an alert (/run/boat-ab/health-alert). Nothing is touched
# when the board came up outside GRUB, or when the disk does not look like
# the layout this image was built for.
#
# Two things GRUB cannot write down itself are dealt with here too:
#   - boat.handover=1: the slot GRUB chose would not load and grubenv could
#     not be written, so GRUB booted the other slot within the same boot.
#     The failed slot is marked unusable (OK=0) here, as GRUB would have,
#     and it is an alert.
#   - boat.esp=N: the version of the boot files on the ESP, which only a USB
#     reflash changes. Older than BOAT_ESP_MIN, what these scripts need, is
#     an alert; older than BOAT_ESP, the newest, is only noted - a reflash
#     empties /data, so it is never pressed on a board for an improvement.
#
# See docs/plans/ab-os-updates.md section 5.3 ("grubenv integrity").
set -eu
SELF=boat-grubenv-guard
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

# ESP versions (boat.esp= from grub.cfg). BOAT_ESP is the newest, bumped
# together with boat_esp in boat-grub-esp's grub.cfg.in. BOAT_ESP_MIN is the
# oldest these scripts work with: raise it only when a release comes to
# depend on a change there.
BOAT_ESP=1
BOAT_ESP_MIN=0

mkdir -p "$BOAT_AB_RUNDIR"
trap '[ "$?" = 0 ] || alert "boat-grubenv-guard failed - the A/B boot state may be wrong (journalctl -b -u boat-grubenv-guard)"' EXIT
mountpoint -q "$BOAT_AB_ESP_MOUNT" || die "the ESP is not mounted at $BOAT_AB_ESP_MOUNT"
check_layout || die "the disk does not match this image's A/B layout - not touching grubenv"

slot="$(booted_slot)"
if [ -n "$slot" ]; then
    esp=0
    for arg in $(cat /proc/cmdline); do
        case "$arg" in boat.esp=*) esp="${arg#boat.esp=}" ;; esac
    done
    case "$esp" in ''|*[!0-9]*) esp=0 ;; esac
    if [ "$esp" -lt "$BOAT_ESP_MIN" ]; then
        alert "the boot files on the ESP are version $esp, and this release needs $BOAT_ESP_MIN: GRUB and grub.cfg only change with a USB reflash - reflash at the dock"
    elif [ "$esp" -lt "$BOAT_ESP" ]; then
        echo "$SELF: the boot files on the ESP are version $esp; the newest is $BOAT_ESP. This release works with them; a USB reflash would bring the newer GRUB and grub.cfg."
    fi
    if cmdline_has boat.handover=1; then
        alert "GRUB could not load slot $(other_slot "$slot") and could not record that, so it booted slot $slot instead, uncounted - slot $(other_slot "$slot") stays unusable until an update rewrites it; if the ESP refuses writes, reflash at the dock"
    fi
fi

ab_lock || die "could not take the A/B lock - another grubenv writer is stuck; nothing written"
if grubenv_valid "$BOAT_AB_GRUBENV"; then
    # Committed, and booted as such - not a trial, not GRUB's last resort
    # (boat-commit decides about that one).
    if [ -n "$slot" ] && ! booted_on_trial && ! booted_lastresort \
            && [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ] \
            && [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_TRY")" != 0 ]; then
        if grubenv_set "${slot}_TRY=0"; then
            say "slot $slot booted: its attempt count is reset"
        else
            alert "could not reset slot $slot's attempt count (the ESP will not take the write) - one more failed boot, or two reboots of any kind while this lasts, hands the board to the other slot"
        fi
    fi
    # The mark GRUB could not write on a hand-over.
    if [ -n "$slot" ] && cmdline_has boat.handover=1; then
        other="$(other_slot "$slot")"
        if [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${other}_OK")" != 0 ]; then
            if grubenv_replace "${other}_OK=0"; then
                say "slot $other failed to load in GRUB: it is marked unusable"
            else
                alert "could not mark slot $other unusable (the ESP will not take the write) - every boot tries it first, then hands over again"
            fi
        fi
    fi
    grubenv_sync_bak || alert "could not copy grubenv to grubenv.bak - GRUB's fallback copy is out of date (the ESP will not take the write)"
    exit 0
fi

[ -n "$slot" ] || die "grubenv is invalid and this boot came from outside GRUB - cannot tell which slot is good"
other="$(other_slot "$slot")"

warn "grubenv is unreadable or inconsistent - rebuilding it"
if booted_on_trial; then
    # GRUB counted this attempt before the kernel loaded.
    slot_env="${slot}_OK=1 ${slot}_TRY=1 ${slot}_COMMITTED=0"
    other_env="${other}_OK=1 ${other}_TRY=0 ${other}_COMMITTED=1"
    note="$slot first and still on trial, $other its committed fallback"
elif booted_lastresort; then
    # Nothing here says whether the slot ever passed a trial, and
    # boat-commit judges a committed slot by Tier 1 only: give it a real
    # trial on the next boot instead. This boot is not counted.
    slot_env="${slot}_OK=1 ${slot}_TRY=0 ${slot}_COMMITTED=0"
    other_env="${other}_OK=0 ${other}_TRY=0 ${other}_COMMITTED=0"
    note="$slot first, booted as GRUB's last resort: on trial from the next boot; $other unusable until an update rewrites it"
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
