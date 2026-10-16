#!/bin/sh
# boat-commit: decide whether the slot this boot came from is healthy, and
# record it in GRUB's state - the health gate of the A/B layout
# (docs/plans/ab-os-updates.md 5.4).
#
#   boat-commit             run by boat-commit.service on every boot
#   boat-commit --deadline  run by boat-trial-deadline.timer, 15 minutes
#                           into a trial: fail the trial if it is still
#                           not committed (a check that hangs, a stuck unit)
#   boat-commit --shutdown  run as boat-trial-inhibit.service stops: a
#                           clean shutdown or reboot before the verdict
#                           gives the trial its attempt back - once
#   boat-commit --may-sleep run before every suspend (a drop-in on
#                           systemd-suspend.service and its kin): fails
#                           while a trial is undecided or an install runs
#
# The last three never read health.conf: an operator's mistake there must
# not also take away the deadline, the shutdown retry or the sleep hold.
#
# Two tiers, so that a broken data partition or an app can never move the
# boat back to an older OS on its own:
#
#   Tier 1, every boot: the kernel came from the slot GRUB named, and that
#     partition has the right GPT name; the rootfs is read-write; the ESP is
#     mounted; grubenv is valid; RAUC answers (when /data is there: RAUC's
#     service waits for it).
#   Tier 2, trial boots only (boat.trial=1): the critical units are active
#     (health.conf), none of the listed units has failed, the update client
#     starts, and - if the operator turned them on - the checks in
#     /data/boat-update/health.d. Docker, and the units listed as needing
#     /data, only when /data is mounted.
#
# The checks are retried for BOAT_HEALTH_DEADLINE seconds, so units that
# come up late are given the chance. A trial is not decided while DATA is
# still being checked or mounted (its checks would be skipped, not passed);
# if it still is at the deadline, the trial is decided without them. Then:
#
#   trial, all pass      commit: OK=1 TRY=0 COMMITTED=1 in one atomic
#                        grubenv write (not RAUC's mark-good, which rewrites
#                        grubenv in place), and the deadline is disarmed -
#                        only once every step of that write succeeded
#   trial, a check fails mark the slot bad (OK=0, the same way), record why
#                        on /data, reboot - GRUB then boots the other slot
#                        (boat.stay=1: no reboot, for bench work)
#   committed boot       the attempt counter is reset first, before any
#                        check (the slot did boot - boat-grubenv-guard has
#                        normally done it already, earlier in the boot); a
#                        Tier 1 failure is an alert, never a reboot or a mark
#   last resort          (boat.lastresort=1: GRUB found no usable slot) an
#                        alert in every case. A committed slot that only ran
#                        out of attempts is made usable again; a slot that
#                        never passed a trial is left as it is
#
# A healthy boot that carried boat.stay=1 ends the hold (BOAT_STAY=0): it
# is for the one boot being debugged, not for good.
#
# Every verdict is a line in /data/boat-update/last-result and in history
# (boat-update history). Every grubenv write takes the A/B lock, so only one
# of these decides. boat-commit.service conflicts with emergency and rescue
# mode: a committed slot that drops into them must not have its attempt
# count reset, or GRUB could never fall back from it.
#
# Nothing outside the board - internet, NTP, GNSS, NMEA - is ever checked.
set -u
SELF=boat-commit
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

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

# DATA is not mounted yet but still on its way: its check (e2fsck, which
# has no time limit) or its mount is running or queued. data.mount is
# nofail, so nothing orders the gate after it.
data_pending() {
    data_mounted && return 1
    for _unit in boat-data-check.service data.mount; do
        case "$(systemctl show -p ActiveState --value "$_unit" 2>/dev/null)" in
            activating|reloading) return 0 ;;
        esac
        [ -n "$(systemctl list-jobs --no-legend "$_unit" 2>/dev/null)" ] && return 0
    done
    return 1
}

# One decision at a time: the gate, the deadline timer and a shutdown must
# never both write grubenv, or commit a slot another has just failed. A
# lock that cannot be had is not waited for twice in one run.
lock_failed=0
lock() {
    [ "$lock_failed" = 1 ] && return 1
    ab_lock || { lock_failed=1; return 1; }
}

slot_partition() {
    case "$1" in
        A) echo "$BOAT_AB_PART_A APP" ;;
        B) echo "$BOAT_AB_PART_B APP_b" ;;
    esac
}

# Active, or made active again: a socket unit that a connection flood
# stopped (systemd's trigger limit) says nothing about the release, so it
# is started again rather than counted against it.
unit_active() {
    systemctl is-active --quiet "$1" && return 0
    case "$1" in *.socket) ;; *) return 1 ;; esac
    [ "$(systemctl show -p Result --value "$1" 2>/dev/null)" = trigger-limit-hit ] || return 1
    warn "$1 was stopped by a flood of connections (its trigger limit) - starting it again"
    systemctl reset-failed "$1" 2>/dev/null
    systemctl start "$1" 2>/dev/null
    systemctl is-active --quiet "$1"
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
        unit_active "$unit" || fail "$unit is not active" || return 1
    done
    # Docker keeps everything on /data; it, and the units the operator
    # listed as needing /data, are only expected when /data is.
    if data_mounted; then
        if systemctl cat docker.service >/dev/null 2>&1; then
            systemctl is-active --quiet docker.service || fail "docker.service is not active" || return 1
        fi
        for unit in $BOAT_HEALTH_DATA_UNITS; do
            unit_active "$unit" || fail "$unit is not active (/data is mounted)" || return 1
        done
    fi
    for unit in $BOAT_HEALTH_FAILED_UNITS; do
        if systemctl is-failed --quiet "$unit"; then
            fail "$unit has failed"
            return 1
        fi
    done
    # The update client, which every later update and every rollback
    # needs: a release that breaks it must not commit.
    if command -v boat-update >/dev/null 2>&1; then
        boat-update --help >/dev/null 2>&1 || fail "the update client does not start (boat-update --help)" || return 1
    fi
    if [ "$BOAT_HEALTH_CHECKS_D" = 1 ] && [ -d "$BOAT_HEALTH_CHECKS_DIR" ]; then
        for check in "$BOAT_HEALTH_CHECKS_DIR"/*; do
            [ -f "$check" ] && [ -x "$check" ] || continue
            # Each check gets what is left before boat-commit.service's own
            # time limit, so the gate always reaches its verdict. Its output
            # goes to this unit's journal.
            left=$(( hard_stop - $(uptime_s) ))
            [ "$left" -gt 0 ] || fail "operator check $check had no time left before boat-commit.service's 10-minute limit" || return 1
            limit="$BOAT_HEALTH_CHECK_TIMEOUT"
            [ "$left" -lt "$limit" ] && limit="$left"
            rc=0
            timeout -k 5 "$limit" "$check" </dev/null || rc=$?
            case "$rc" in
                0) ;;
                124|137) fail "operator check $check did not finish within $limit s (journalctl -b -u boat-commit has its output)"; return 1 ;;
                *) fail "operator check $check failed, exit $rc (journalctl -b -u boat-commit has its output)"; return 1 ;;
            esac
        done
    fi
    return 0
}

# One line of history on /data - last-result, and appended to history - best
# effort: /data may be what is broken - and then nothing is written, rather
# than onto the slot's own rootfs under the bare mount point, where nothing
# would ever read it.
record() {
    if data_mounted && mkdir -p "$STATE_DIR" 2>/dev/null; then
        line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
        printf '%s\n' "$line" > "$STATE_DIR/last-result.new" \
            && mv "$STATE_DIR/last-result.new" "$STATE_DIR/last-result" \
            || warn "could not write $STATE_DIR/last-result"
        printf '%s\n' "$line" >> "$STATE_DIR/history" \
            || warn "could not write $STATE_DIR/history"
    fi
}

# The OTA check never offers a version that failed a trial here again.
blacklist() {
    if data_mounted && mkdir -p "$STATE_DIR" 2>/dev/null; then
        release >> "$STATE_DIR/failed-versions" || true
    fi
}

release() { cat /etc/boat-release 2>/dev/null || echo unknown; }

# This boot's commit, written only once grubenv_set has succeeded. The
# deadline goes by it, not by COMMITTED=1 in grubenv: a write that failed
# after its rename shows COMMITTED=1 that was never flushed, or that
# grubenv.bak - GRUB's fallback - does not have.
COMMITTED_MARK="$BOAT_AB_RUNDIR/committed"
# This boot's trial failed: nothing may give it its attempt back.
FAILED_MARK="$BOAT_AB_RUNDIR/trial-failed"
# This boot's trial passed its checks, but the commit could not be
# written (the lock, the ESP): why, for the deadline's record.
COMMIT_FAILED_MARK="$BOAT_AB_RUNDIR/commit-failed"

# BOAT_STAY=0 once a boot that carried the hold is found healthy.
stay_off() {
    cmdline_has boat.stay=1 && echo BOAT_STAY=0
}

commit_failed() {
    printf '%s\n' "$1" > "$COMMIT_FAILED_MARK" 2>/dev/null || true
    die "$1 - slot $slot is NOT committed (the trial deadline, or GRUB's attempt count, ends this trial)"
}

commit() {
    lock || commit_failed "could not take the A/B lock"
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_OK")" = 1 ] \
        || die "slot $slot was failed meanwhile (the trial deadline) - not committing it"
    # BOAT_RETRY: the next trial gets its own shutdown retry.
    # shellcheck disable=SC2046 # no word, or one
    grubenv_set "${slot}_OK=1" "${slot}_TRY=0" "${slot}_COMMITTED=1" BOAT_RETRY= $(stay_off) \
        && [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ] \
        || commit_failed "could not write grubenv"
    : > "$COMMITTED_MARK" || warn "could not write $COMMITTED_MARK - the trial deadline will fail this slot"
    systemctl stop boat-trial-deadline.timer 2>/dev/null || true
    systemctl stop boat-trial-inhibit.service 2>/dev/null || true
    say "slot $slot ($(release)) passed its trial: committed"
    record "committed slot $slot ($(release))"
}

# A trial failed for a reason the board itself can cause too (Tier 1, a
# commit that could not be written) is still failed - boat-commit cannot
# tell a damaged ESP from a release that broke it - but its record says
# how to try that release again.
board_fault=0
fail_trial() {
    mkdir -p "$BOAT_AB_RUNDIR" 2>/dev/null
    : > "$FAILED_MARK" 2>/dev/null || true
    warn "slot $slot ($(release)) failed its trial: $1"
    if lock; then
        grubenv_set "${slot}_OK=0" "${slot}_TRY=0" \
            || warn "could not mark slot $slot bad - GRUB's own attempt count still ends this trial"
    else
        warn "not marking slot $slot bad without the A/B lock - GRUB's own attempt count ends this trial at the reboot"
    fi
    if [ "$board_fault" = 1 ]; then
        record "trial of slot $slot ($(release)) FAILED: $1 - if the board, not the release, was at fault, 'boat-update install <bundle file>' installs it again once that is fixed"
    else
        record "trial of slot $slot ($(release)) FAILED: $1"
    fi
    blacklist
    sync
    if cmdline_has boat.stay=1; then
        warn "boat.stay=1: staying up for inspection - reboot to fall back to the other slot"
        exit 1
    fi
    warn "rebooting: GRUB will boot the other slot"
    systemctl reboot
    exit 1
}

trial=0
booted_on_trial && trial=1

# No sleep while a trial is undecided - the trial deadline counts real
# time, and a power cut while asleep would cost the release its attempt -
# nor while an install writes the other slot. The inhibitor locks behind
# this are root's, and logind never holds root back with root's own locks,
# so this runs before every suspend, whoever asked. Fails closed: a state
# that cannot be read refuses.
if [ "${1:-}" = "--may-sleep" ]; then
    if [ "$trial" = 1 ] && [ ! -e "$COMMITTED_MARK" ]; then
        die "refusing to sleep: this boot is an A/B update on trial, not committed yet ('boat-update status'; it takes a minute or two)"
    fi
    systemctl is-active --quiet boat-update-install.service 2>/dev/null
    case "$?" in
        3) ;;
        0) die "refusing to sleep: an A/B update is being installed ('journalctl -u boat-update-install')" ;;
        *) die "refusing to sleep: cannot tell whether an A/B update is being installed" ;;
    esac
    exit 0
fi

slot="$(booted_slot)"
if [ -z "$slot" ]; then
    say "booted outside GRUB (no rauc.slot=) - nothing to commit"
    exit 0
fi

if [ "${1:-}" = "--deadline" ]; then
    [ "$trial" = 1 ] || exit 0
    # Without the lock the trial still ends (fail_trial reboots, and GRUB's
    # attempt count does the rest).
    lock || true
    [ -e "$COMMITTED_MARK" ] && exit 0
    # Failed already (boat.stay=1 keeps the board up after that): that
    # verdict, and the reason it recorded, stand.
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_OK")" = 0 ] && exit 0
    if [ -e "$COMMIT_FAILED_MARK" ]; then
        board_fault=1
        fail_trial "it passed its checks, but its commit was not written ($(cat "$COMMIT_FAILED_MARK" 2>/dev/null))"
    fi
    if [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ]; then
        board_fault=1
        fail_trial "its commit was never confirmed (the grubenv write failed part-way)"
    fi
    fail_trial "not committed within the trial deadline"
fi

# A clean shutdown or reboot before the verdict - the helm's power-off, the
# power key - says nothing about the release, but GRUB has already used the
# trial's one attempt, so the next boot would fail it and the release would
# never be offered again. Give the attempt back, once per trial
# (BOAT_RETRY): a release that keeps being shut down mid-trial still ends.
# Not after a verdict, not for a forced reboot (systemctl --force skips
# this), and not when boat-commit stops the inhibitor on commit.
if [ "${1:-}" = "--shutdown" ]; then
    [ "$trial" = 1 ] || exit 0
    [ -e "$COMMITTED_MARK" ] && exit 0
    [ -e "$FAILED_MARK" ] && exit 0
    [ "$(systemctl is-system-running 2>/dev/null)" = stopping ] || exit 0
    # Without the lock, no retry: the attempt counts, as a power cut's does.
    lock || exit 0
    [ -e "$COMMITTED_MARK" ] && exit 0
    [ -e "$FAILED_MARK" ] && exit 0
    grubenv_valid "$BOAT_AB_GRUBENV" || exit 0
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_OK")" = 1 ] || exit 0
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 0 ] || exit 0
    # GRUB boots the other slot next: 'boat-update rollback' moved the
    # board off this trial before its verdict. That is a verdict too - the
    # release was rejected - so it is marked bad and not offered again,
    # rather than given its attempt back behind the slot rolled back to.
    other="$(other_slot "$slot")"
    first="$(grubenv_get "$BOAT_AB_GRUBENV" ORDER)"
    if [ "${first%% *}" = "$other" ] && [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${other}_OK")" = 1 ]; then
        : > "$FAILED_MARK" 2>/dev/null || true
        grubenv_set "${slot}_OK=0" "${slot}_TRY=0" \
            || warn "could not mark slot $slot bad - GRUB's own attempt count still ends this trial"
        warn "left slot $slot's trial for slot $other before its verdict (a rollback) - slot $slot is marked bad"
        record "trial of slot $slot ($(release)) ended by a rollback to slot $other before its verdict - marked bad, not offered again"
        blacklist
        sync
        exit 0
    fi
    if [ "$(grubenv_get "$BOAT_AB_GRUBENV" BOAT_RETRY)" = "$slot" ]; then
        warn "shut down before slot $slot's trial was decided, again - this attempt counts: the next boot falls back"
        record "trial of slot $slot ($(release)) shut down before its verdict, a second time - it is not retried"
        exit 0
    fi
    if grubenv_set "${slot}_TRY=0" "BOAT_RETRY=$slot"; then
        say "shut down before slot $slot's trial was decided - the next boot tries it again (once)"
        record "trial of slot $slot ($(release)) shut down before its verdict - retried on the next boot"
    else
        warn "could not give slot $slot's trial its attempt back - the next boot falls back"
    fi
    exit 0
fi

# --- configuration (/etc/boat-update/health.conf overrides these) ---
BOAT_HEALTH_UNITS="NetworkManager.service systemd-logind.service dbus.service boat-bootmode-guard.service sshd.socket"
BOAT_HEALTH_DATA_UNITS=""
BOAT_HEALTH_FAILED_UNITS=""
BOAT_HEALTH_CHECKS_D=0
BOAT_HEALTH_CHECKS_DIR=/data/boat-update/health.d
BOAT_HEALTH_CHECK_TIMEOUT=60
BOAT_HEALTH_DEADLINE=540
HEALTH_CONF=/etc/boat-update/health.conf
# Tried in a subshell first: a line that would end this script (an unset
# variable under set -u, a syntax error) costs the operator's settings,
# not the verdict - and says so on every boot, so it shows on the committed
# slot before an update carries the file into the next one.
if [ -r "$HEALTH_CONF" ]; then
    # shellcheck source=/dev/null
    if ( . "$HEALTH_CONF" ) >/dev/null 2>&1; then
        # shellcheck source=/dev/null
        . "$HEALTH_CONF"
    else
        alert "$HEALTH_CONF cannot be read as shell (an unset variable or a syntax error) - the health gate uses its defaults until it is fixed"
    fi
fi

# The checks run inside boat-commit.service's 10-minute limit.
case "$BOAT_HEALTH_DEADLINE" in
    ''|*[!0-9]*) warn "BOAT_HEALTH_DEADLINE='$BOAT_HEALTH_DEADLINE' is not a number of seconds - using 540"
                 BOAT_HEALTH_DEADLINE=540 ;;
esac
if [ "$BOAT_HEALTH_DEADLINE" -gt 540 ]; then
    warn "BOAT_HEALTH_DEADLINE=$BOAT_HEALTH_DEADLINE is past what boat-commit.service allows - using 540"
    BOAT_HEALTH_DEADLINE=540
fi
case "$BOAT_HEALTH_CHECK_TIMEOUT" in
    ''|0|*[!0-9]*) warn "BOAT_HEALTH_CHECK_TIMEOUT='$BOAT_HEALTH_CHECK_TIMEOUT' is not a number of seconds - using 60"
                   BOAT_HEALTH_CHECK_TIMEOUT=60 ;;
esac
# Where boat-commit.service's TimeoutStartSec (10 minutes) would stop this
# run, less time to write the verdict: no check may run past it.
hard_stop=$(( $(uptime_s) + 590 ))

lastresort=0
booted_lastresort && lastresort=1
committed="$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")"
problems=""
problem() { problems="${problems:+$problems; }$*"; }

if [ "$lastresort" = 1 ]; then
    if [ "$committed" = 1 ]; then
        problem "GRUB found no usable slot and booted slot $slot as its last resort - it is committed and only ran out of attempts, so it is made usable again"
    else
        problem "GRUB found no usable slot and booted slot $slot as its last resort - it has not passed a trial (a failed release or an interrupted install): install a release, or reflash"
    fi
fi

# A committed slot that got this far did boot: reset its attempt count
# first, before any check that may take a while, so only a slot that keeps
# failing to BOOT is ever handed over. A slot on trial, or one that never
# passed a trial and only booted as the last resort, is never made usable
# here.
if [ "$trial" = 0 ] && [ "$committed" = 1 ] && grubenv_valid "$BOAT_AB_GRUBENV" \
        && { [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_OK")" != 1 ] \
             || [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_TRY")" != 0 ]; }; then
    if lock; then
        grubenv_set "${slot}_OK=1" "${slot}_TRY=0" \
            || problem "could not reset slot $slot's attempt count (the ESP will not take the write) - one more failed boot, or two reboots of any kind while this lasts, hands the board to the other slot"
        ab_unlock
    else
        problem "could not take the A/B lock to reset slot $slot's attempt count - one more failed boot hands the board to the other slot"
    fi
fi

# /proc/uptime keeps counting while the board sleeps, and the trial
# deadline's timer does not: a sleep (which nothing should allow during a
# trial) gives the checks their time again rather than ending them at once.
deadline=$(( $(uptime_s) + BOAT_HEALTH_DEADLINE ))
data_waiting=0
while :; do
    reason=""
    if ! tier1; then
        board_fault=1
    elif [ "$trial" = 1 ] && ! tier2; then
        board_fault=0
    elif [ "$trial" = 1 ] && data_pending; then
        # Passed - but only because /data, and with it RAUC, Docker and
        # health.d, is not there yet. Wait for DATA; at the deadline it is
        # a data problem, which never fails the OS.
        if [ "$(uptime_s)" -ge "$deadline" ]; then
            warn "DATA is still being checked or mounted - deciding without RAUC, Docker and the checks that need /data"
            break
        fi
        if [ "$data_waiting" = 0 ]; then
            say "waiting for DATA (its check or mount is still running) before deciding the trial"
            data_waiting=1
        fi
    else
        break
    fi
    [ "$(uptime_s)" -ge "$deadline" ] && break
    before=$(uptime_s)
    sleep 5
    slept=$(( $(uptime_s) - before - 5 ))
    if [ "$slept" -gt 60 ]; then
        say "the board was asleep for about $slept s - the checks get that time back"
        deadline=$(( deadline + slept ))
        hard_stop=$(( hard_stop + slept ))
    fi
done

if [ "$trial" = 1 ]; then
    [ -z "$reason" ] && { commit; exit 0; }
    fail_trial "$reason"
fi

[ -n "$reason" ] && problem "slot $slot is unhealthy: $reason - staying on it (see 'journalctl -b -u boat-commit')"
if [ -n "$problems" ]; then
    alert "$problems"
    record "slot $slot ($(release)): $problems"
    exit 1
fi
if [ -n "$(stay_off)" ] && [ "$(grubenv_get "$BOAT_AB_GRUBENV" BOAT_STAY)" = 1 ]; then
    if lock && grubenv_set BOAT_STAY=0; then
        say "debug hold (BOAT_STAY) ended: this boot was healthy"
    else
        warn "could not end the debug hold (BOAT_STAY=1)"
    fi
fi
say "slot $slot ($(release)) is healthy"
exit 0
