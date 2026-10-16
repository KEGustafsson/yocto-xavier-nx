# shellcheck shell=sh
# Sourced; the scripts that source it read the variables set here.
# shellcheck disable=SC2034
# Shared by boat-bootmode-guard, boat-grubenv-guard, boat-commit, RAUC's
# install handlers and boat-storage.
# Installed as /usr/lib/boat-ab/lib.sh with the build's layout values filled
# in; see docs/plans/ab-os-updates.md sections 5.1-5.4.

BOAT_AB_DISK="@DISK@"
BOAT_AB_PART_A="@PARTNUM_A@"
BOAT_AB_PART_B="@PARTNUM_B@"
BOAT_AB_PART_ESP="@PARTNUM_ESP@"
BOAT_AB_PART_SWAP="@PARTNUM_SWAP@"
BOAT_AB_PART_DATA="@PARTNUM_DATA@"
BOAT_AB_ESP_MOUNT=/boot/efi
BOAT_AB_GRUBENV="${BOAT_AB_ESP_MOUNT}/EFI/BOOT/grubenv"
BOAT_AB_RUNDIR=/run/boat-ab
# Problems this boot that need a person, one per line: shown by
# 'boat-update status'.
BOAT_AB_ALERT="${BOAT_AB_RUNDIR}/health-alert"
# gNVIDIAPublicVariableGuid - L4TDefaultBootMode and the rootfs slot status.
BOAT_AB_NV_GUID=781e084c-a330-417c-b678-38e696380cb9
EFIVARS=/sys/firmware/efi/efivars

# Every variable grub.cfg reads, and the values each may hold.
BOAT_AB_ENV_KEYS="ORDER A_OK A_TRY A_COMMITTED B_OK B_TRY B_COMMITTED BOAT_STAY"

# POSIX sh has no local variables: every variable a function here sets is
# global, and would silently overwrite a caller's variable of the same name.
# So all of them start with _ab_, a prefix no script that sources this uses.

say() { echo "${SELF:-boat-ab}: $*"; }
warn() { echo "${SELF:-boat-ab}: WARNING: $*" >&2; }
die() { echo "${SELF:-boat-ab}: $*" >&2; exit 1; }

# Warn, and add the line to this boot's alerts. Never fails.
alert() {
    warn "$*"
    { mkdir -p "$BOAT_AB_RUNDIR" && echo "$*" >> "$BOAT_AB_ALERT"; } 2>/dev/null || true
}

# One grubenv read-modify-write at a time: boat-commit, the guards, RAUC
# (through the grub-editenv wrapper) and boat-update all take this lock.
# Held on fd 9 until ab_unlock or exit. It fails after BOAT_AB_LOCK_WAIT
# seconds - a writer that went on without it could undo the decision of
# the one holding it (a commit over a deadline's verdict) - and the caller
# then writes nothing: GRUB's own attempt count is the safe answer.
BOAT_AB_LOCK_WAIT=60
_ab_locked=0
ab_lock() {
    [ "$_ab_locked" = 1 ] && return 0
    mkdir -p "$BOAT_AB_RUNDIR"
    exec 9>"$BOAT_AB_RUNDIR/commit.lock"
    if ! flock -w "$BOAT_AB_LOCK_WAIT" 9; then
        exec 9>&-
        warn "could not take $BOAT_AB_RUNDIR/commit.lock in $BOAT_AB_LOCK_WAIT s - another grubenv writer is stuck"
        return 1
    fi
    _ab_locked=1
}
ab_unlock() {
    exec 9>&-
    _ab_locked=0
}

# True when word $1 is on the kernel command line.
cmdline_has() {
    for _ab_arg in $(cat /proc/cmdline); do
        [ "$_ab_arg" = "$1" ] && return 0
    done
    return 1
}

# The slot GRUB booted (A or B), from rauc.slot= on the kernel command line.
# Empty when the board came up outside GRUB (L4TLauncher's own fallback).
booted_slot() {
    for _ab_arg in $(cat /proc/cmdline); do
        case "$_ab_arg" in
            rauc.slot=A) echo A; return 0 ;;
            rauc.slot=B) echo B; return 0 ;;
        esac
    done
    return 0
}

other_slot() {
    case "$1" in
        A) echo B ;;
        B) echo A ;;
    esac
}

part_dev() { echo "${BOAT_AB_DISK}p$1"; }

# GPT entry name of /dev/<disk>p<N>, straight from the partition table.
part_name() {
    blkid -p -o value -s PART_ENTRY_NAME "$(part_dev "$1")" 2>/dev/null
}

# True when GPT entry $1 is named $2. Prints the mismatch otherwise.
check_part() {
    _ab_have="$(part_name "$1")"
    [ "$_ab_have" = "$2" ] && return 0
    warn "$(part_dev "$1") is GPT entry '${_ab_have:-<none>}', expected '$2'"
    return 1
}

# Wait up to $2 seconds for block device $1 to appear.
wait_dev() {
    _ab_n=0
    while [ ! -b "$1" ]; do
        [ "$_ab_n" -ge "$2" ] && return 1
        sleep 1
        _ab_n=$((_ab_n + 1))
    done
    return 0
}

# Refuse to act unless the numbers this build was configured with still
# name the partitions they are meant to. Prints the problem and returns 1.
check_layout() {
    _ab_ok=0
    check_part "${BOAT_AB_PART_A}" APP || _ab_ok=1
    check_part "${BOAT_AB_PART_B}" APP_b || _ab_ok=1
    check_part "${BOAT_AB_PART_ESP}" esp || _ab_ok=1
    return $_ab_ok
}

# The u32 value of a gNVIDIAPublicVariableGuid UEFI variable, or "absent".
# efivarfs files are a 4-byte attribute word followed by the data.
efi_u32() {
    _ab_f="${EFIVARS}/$1-${BOAT_AB_NV_GUID}"
    if [ ! -e "$_ab_f" ]; then
        echo absent
        return 0
    fi
    od -An -t u4 -j 4 -N 4 "$_ab_f" | tr -d ' '
}

# The same for a one-byte variable.
efi_u8() {
    _ab_f="${EFIVARS}/$1-${BOAT_AB_NV_GUID}"
    if [ ! -e "$_ab_f" ]; then
        echo absent
        return 0
    fi
    od -An -t u1 -j 4 -N 1 "$_ab_f" | tr -d ' '
}

# True when grubenv at $1 parses and every variable grub.cfg reads holds a
# value it understands.
grubenv_valid() {
    _ab_env="$(grub-editenv "$1" list 2>/dev/null)" || return 1
    for _ab_key in $BOAT_AB_ENV_KEYS; do
        _ab_val="$(printf '%s\n' "$_ab_env" | sed -n "s/^${_ab_key}=//p")"
        case "$_ab_key:$_ab_val" in
            ORDER:"A B"|ORDER:"B A") ;;
            *_OK:0|*_OK:1|*_COMMITTED:0|*_COMMITTED:1|BOAT_STAY:0|BOAT_STAY:1) ;;
            *_TRY:0|*_TRY:1|*_TRY:2) ;;
            *) return 1 ;;
        esac
    done
    return 0
}

grubenv_get() {
    grub-editenv "$1" list 2>/dev/null | sed -n "s/^$2=//p"
}

# True when this boot is a trial: GRUB adds boat.trial=1 for a slot that is
# not committed yet.
booted_on_trial() { cmdline_has boat.trial=1; }

# True when GRUB found no usable slot and booted this one as its last
# resort (boat.lastresort=1): uncounted, whatever state the slot is in.
booted_lastresort() { cmdline_has boat.lastresort=1; }

# Replace file $2 with file $1 so that a power cut leaves either the old or
# the new contents, never a mix: flush, rename, flush. (grub-editenv itself
# truncates and rewrites in place.) On the ESP's vfat a rename frees the old
# file's clusters at once but only marks the moved file's directory entry
# dirty, so until writeback that entry still points at the freed clusters:
# fsync the renamed file itself, which writes the entry, then the directory.
# Every step must succeed: callers act on the result (and few run set -e).
replace_file() {
    sync "$1" && mv "$1" "$2" && sync "$2" && sync "$(dirname "$2")"
}

# Set KEY=VALUE pairs in grubenv, on a copy that then replaces it. Fails,
# leaving grubenv as it was, if any step fails.
grubenv_replace() {
    rm -f "${BOAT_AB_GRUBENV}.new"
    if ! { cp "$BOAT_AB_GRUBENV" "${BOAT_AB_GRUBENV}.new" \
            && grub-editenv "${BOAT_AB_GRUBENV}.new" set "$@" \
            && replace_file "${BOAT_AB_GRUBENV}.new" "$BOAT_AB_GRUBENV"; }; then
        rm -f "${BOAT_AB_GRUBENV}.new"
        return 1
    fi
}

# The same, then bring grubenv.bak - GRUB's fallback if grubenv is ever
# torn - up to date with it. Fails if either step fails: when only the .bak
# copy did, grubenv has already changed (grubenv_replace tells the two apart).
grubenv_set() {
    grubenv_replace "$@" && grubenv_sync_bak
}

# Make grubenv.bak a copy of grubenv, if it is not one already.
grubenv_sync_bak() {
    cmp -s "$BOAT_AB_GRUBENV" "${BOAT_AB_GRUBENV}.bak" && return 0
    if ! { cp "$BOAT_AB_GRUBENV" "${BOAT_AB_GRUBENV}.bak.new" \
            && replace_file "${BOAT_AB_GRUBENV}.bak.new" "${BOAT_AB_GRUBENV}.bak"; }; then
        rm -f "${BOAT_AB_GRUBENV}.bak.new"
        return 1
    fi
}
