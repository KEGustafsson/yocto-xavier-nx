#!/bin/sh
# boat-commit: decide whether the slot this boot came from is healthy, and
# record it in GRUB's state - the health gate of the A/B layout
# (docs/plans/ab-os-updates.md 5.4).
#
#   boat-commit             run by boat-commit.service on every boot
#   boat-commit --deadline  run by boat-trial-deadline.timer, 15 minutes
#                           into a trial: fail the trial if it is still
#                           not committed (a check that hangs, a stuck unit)
#
# Two tiers, so that a broken data partition or an app can never move the
# boat back to an older OS on its own:
#
#   Tier 1, every boot: the kernel came from the slot GRUB named, and that
#     partition has the right GPT name; the rootfs is read-write; the ESP is
#     mounted; grubenv is valid; RAUC answers (when /data is there: RAUC's
#     service waits for it).
#   Tier 2, trial boots only (boat.trial=1): the critical units are active
#     (health.conf), none of the listed units has failed, and - if the
#     operator turned them on - the checks in /data/boat-update/health.d.
#
# The checks are retried for BOAT_HEALTH_DEADLINE seconds, so units that
# come up late are given the chance. Then:
#
#   trial, all pass      commit: OK=1 TRY=0 COMMITTED=1 in one atomic
#                        grubenv write (not RAUC's mark-good, which rewrites
#                        grubenv in place), and the deadline is disarmed -
#                        only once every step of that write succeeded
#   trial, a check fails mark the slot bad (OK=0, the same way), record why
#                        on /data, reboot - GRUB then boots the other slot
#                        (boat.stay=1: no reboot, for bench work)
#   committed boot       the attempt counter is reset first, before any
#                        check (the slot did boot); a Tier 1 failure is an
#                        alert, never a reboot or a mark
#
# The gate and the deadline take a lock, so only one of them decides.
#
# Nothing outside the board - internet, NTP, GNSS, NMEA - is ever checked.
set -u
SELF=boat-commit
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

# --- configuration (/etc/boat-update/health.conf overrides these) ---
BOAT_HEALTH_UNITS="NetworkManager.service systemd-logind.service dbus.service boat-bootmode-guard.service sshd.socket"
BOAT_HEALTH_FAILED_UNITS=""
BOAT_HEALTH_CHECKS_D=0
BOAT_HEALTH_CHECKS_DIR=/data/boat-update/health.d
BOAT_HEALTH_CHECK_TIMEOUT=60
BOAT_HEALTH_DEADLINE=540
HEALTH_CONF=/etc/boat-update/health.conf
# shellcheck source=/dev/null
[ -r "$HEALTH_CONF" ] && . "$HEALTH_CONF"

# The checks run inside boat-commit.service's 10-minute limit.
case "$BOAT_HEALTH_DEADLINE" in
    ''|*[!0-9]*) warn "BOAT_HEALTH_DEADLINE='$BOAT_HEALTH_DEADLINE' is not a number of seconds - using 540"
                 BOAT_HEALTH_DEADLINE=540 ;;
esac
if [ "$BOAT_HEALTH_DEADLINE" -gt 540 ]; then
    warn "BOAT_HEALTH_DEADLINE=$BOAT_HEALTH_DEADLINE is past what boat-commit.service allows - using 540"
    BOAT_HEALTH_DEADLINE=540
fi

STATE_DIR=/data/boat-update
DATA_MOUNT=/data

reason=""
fail() { reason="$*"; return 1; }

uptime_s() { cut -d. -f1 /proc/uptime; }

mount_source() {
    awk -v m="$1" '$2 == m { s = $1 } END { print s }' /proc/mounts
}

mount_options() {
    awk -v m="$1" '$2 == m { o = $4 } END { print o }' /proc/mounts
}

# /data really is the DATA partition (not the bare mount point on the rootfs).
data_mounted() {
    [ "$(mount_source "$DATA_MOUNT")" = "$(part_dev "$BOAT_AB_PART_DATA")" ]
}

# One decision at a time: the gate and the deadline timer must never both
# write grubenv, or commit a slot the other has just failed.
locked=0
lock() {
    [ "$locked" = 1 ] && return 0
    mkdir -p "$BOAT_AB_RUNDIR"
    exec 9>"$BOAT_AB_RUNDIR/commit.lock"
    flock -w 60 9 || warn "could not take $BOAT_AB_RUNDIR/commit.lock - going on without it"
    locked=1
}

slot_partition() {
    case "$1" in
        A) echo "$BOAT_AB_PART_A APP" ;;
        B) echo "$BOAT_AB_PART_B APP_b" ;;
    esac
}

tier1() {
    # shellcheck disable=SC2046 # two words, on purpose
    set -- $(slot_partition "$slot")
    [ "$(mount_source /)" = "$(part_dev "$1")" ] \
        || fail "/ is $(mount_source /), not slot $slot's $(part_dev "$1")" || return 1
    check_part "$1" "$2" 2>/dev/null \
        || fail "$(part_dev "$1") is not the GPT entry '$2'" || return 1
    case ",$(mount_options /)," in
        *,rw,*) ;;
        *) fail "the root filesystem is not read-write"; return 1 ;;
    esac
    mountpoint -q "$BOAT_AB_ESP_MOUNT" || fail "the ESP is not mounted at $BOAT_AB_ESP_MOUNT" || return 1
    grubenv_valid "$BOAT_AB_GRUBENV" || fail "grubenv is not valid" || return 1
    # rauc talks to rauc.service, which waits for /data: without /data that
    # is a data problem, which must not fail the OS.
    if command -v rauc >/dev/null 2>&1 && data_mounted; then
        rauc status >/dev/null 2>&1 || fail "'rauc status' does not answer" || return 1
    fi
    return 0
}

tier2() {
    for unit in $BOAT_HEALTH_UNITS; do
        systemctl is-active --quiet "$unit" || fail "$unit is not active" || return 1
    done
    # Docker keeps everything on /data; it is only expected when /data is.
    if data_mounted && systemctl cat docker.service >/dev/null 2>&1; then
        systemctl is-active --quiet docker.service || fail "docker.service is not active" || return 1
    fi
    for unit in $BOAT_HEALTH_FAILED_UNITS; do
        if systemctl is-failed --quiet "$unit"; then
            fail "$unit has failed"
            return 1
        fi
    done
    if [ "$BOAT_HEALTH_CHECKS_D" = 1 ] && [ -d "$BOAT_HEALTH_CHECKS_DIR" ]; then
        for check in "$BOAT_HEALTH_CHECKS_DIR"/*; do
            [ -f "$check" ] && [ -x "$check" ] || continue
            timeout "$BOAT_HEALTH_CHECK_TIMEOUT" "$check" >/dev/null 2>&1 \
                || fail "operator check $check failed" || return 1
        done
    fi
    return 0
}

# One line of history on /data, best effort: /data may be what is broken -
# and then nothing is written, rather than onto the slot's own rootfs under
# the bare mount point, where nothing would ever read it.
record() {
    if data_mounted && mkdir -p "$STATE_DIR" 2>/dev/null; then
        printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATE_DIR/last-result.new" \
            && mv "$STATE_DIR/last-result.new" "$STATE_DIR/last-result" \
            || warn "could not write $STATE_DIR/last-result"
    fi
}

release() { cat /etc/boat-release 2>/dev/null || echo unknown; }

# This boot's commit, written only once grubenv_set has succeeded. The
# deadline goes by it, not by COMMITTED=1 in grubenv: a write that failed
# after its rename shows COMMITTED=1 that was never flushed, or that
# grubenv.bak - GRUB's fallback - does not have.
COMMITTED_MARK="$BOAT_AB_RUNDIR/committed"

commit() {
    lock
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_OK")" = 1 ] \
        || die "slot $slot was failed meanwhile (the trial deadline) - not committing it"
    grubenv_set "${slot}_OK=1" "${slot}_TRY=0" "${slot}_COMMITTED=1" \
        && [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ] \
        || die "could not write grubenv - slot $slot is NOT committed (the trial deadline fails it)"
    : > "$COMMITTED_MARK" || warn "could not write $COMMITTED_MARK - the trial deadline will fail this slot"
    systemctl stop boat-trial-deadline.timer 2>/dev/null || true
    systemctl stop boat-trial-inhibit.service 2>/dev/null || true
    say "slot $slot ($(release)) passed its trial: committed"
    record "committed slot $slot ($(release))"
}

fail_trial() {
    lock
    warn "slot $slot ($(release)) failed its trial: $1"
    grubenv_set "${slot}_OK=0" "${slot}_TRY=0" \
        || warn "could not mark slot $slot bad - GRUB's own attempt count still ends this trial"
    record "trial of slot $slot ($(release)) FAILED: $1"
    # The OTA check never offers a version that failed a trial here again.
    if data_mounted && mkdir -p "$STATE_DIR" 2>/dev/null; then
        release >> "$STATE_DIR/failed-versions" || true
    fi
    sync
    for arg in $(cat /proc/cmdline); do
        if [ "$arg" = boat.stay=1 ]; then
            warn "boat.stay=1: staying up for inspection - reboot to fall back to the other slot"
            exit 1
        fi
    done
    warn "rebooting: GRUB will boot the other slot"
    systemctl reboot
    exit 1
}

slot="$(booted_slot)"
if [ -z "$slot" ]; then
    say "booted outside GRUB (no rauc.slot=) - nothing to commit"
    exit 0
fi
trial=0
booted_on_trial && trial=1

if [ "${1:-}" = "--deadline" ]; then
    [ "$trial" = 1 ] || exit 0
    lock
    [ -e "$COMMITTED_MARK" ] && exit 0
    if [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ]; then
        fail_trial "its commit was never confirmed (the grubenv write failed part-way)"
    fi
    fail_trial "not committed within the trial deadline"
fi

# A committed slot that got this far did boot: reset its attempt count
# first, before any check that may take a while, so only a slot that keeps
# failing to BOOT is ever handed over.
if [ "$trial" = 0 ] && grubenv_valid "$BOAT_AB_GRUBENV"; then
    lock
    grubenv_set "${slot}_OK=1" "${slot}_TRY=0" \
        || warn "could not reset slot $slot's attempt count - two more failed boots would hand the board to the other slot"
    exec 9>&-
    locked=0
fi

deadline=$(( $(uptime_s) + BOAT_HEALTH_DEADLINE ))
while :; do
    reason=""
    if tier1 && { [ "$trial" = 0 ] || tier2; }; then
        break
    fi
    [ "$(uptime_s)" -ge "$deadline" ] && break
    sleep 5
done

if [ "$trial" = 1 ]; then
    [ -z "$reason" ] && { commit; exit 0; }
    fail_trial "$reason"
fi

if [ -n "$reason" ]; then
    mkdir -p "$BOAT_AB_RUNDIR"
    echo "$reason" > "$BOAT_AB_RUNDIR/health-alert"
    warn "slot $slot is committed but unhealthy: $reason - staying on it (see 'journalctl -b -u boat-commit')"
    record "slot $slot ($(release)) committed but unhealthy: $reason"
    exit 1
fi
say "slot $slot ($(release)) is healthy"
exit 0
