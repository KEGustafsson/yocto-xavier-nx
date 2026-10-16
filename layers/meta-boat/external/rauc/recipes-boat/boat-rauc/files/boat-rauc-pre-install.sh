#!/bin/sh
# RAUC system pre-install handler for the boat computer's A/B layout
# (docs/plans/ab-os-updates.md 5.6, step 2). RAUC 1.13 runs it after the
# bundle's signature and format have been checked but BEFORE its compatible
# string and min-bundle-version are, and before anything is written; a
# non-zero exit stops the install with nothing changed. It refuses unless:
#
#   - the board came up through GRUB (rauc.slot= on the command line), from
#     a slot that is committed: no install starts during a trial. (GRUB's
#     last resort, boat.lastresort=1, boots whatever there is - an install
#     is how such a board is mended, so it may install.);
#   - when boat-update runs the install, the bundle is the release it is
#     installing (/run/boat-update/expect): a channel that serves another
#     signed bundle than its index offered gets nothing written;
#   - /data is the DATA partition (RAUC's data directory lives there), and
#     has room for RAUC to save its status: RAUC marks the target unusable
#     BEFORE its first save there, so a full /data would cost the fallback
#     slot and install nothing;
#   - every partition RAUC is about to write is the one this layout puts at
#     that number (GPT name APP or APP_b), and is not the booted slot;
#   - the bundle was built for the L4T release this board runs - a BSP bump
#     changes firmware RAUC does not update, and needs a USB reflash - and
#     against the same kernel device tree as this slot (firmware.id): the
#     kernel runs on the DTB written at flash, never on one from a slot;
#   - RAUC's own checks that come after this handler will pass: the
#     bundle's compatible is this board's, and its version is not below
#     system.conf's min-bundle-version (an install forced past that with
#     --ignore-version-limit is refused here too: lower the floor instead).
#
# Board settings changed in the target slot after the later of the two
# slots' installs (a rollback or a fallback ran it) would be lost when it is
# overwritten: they are kept in /data/boat-update/displaced, with an alert.
#
# Then it marks the target slot not committed - and only that. Nothing
# should refuse the bundle after this, but if something does (a failed
# write) the slot must stay usable as the fallback it is. RAUC itself marks
# the target unusable (OK=0) right before it writes it, and its activation
# leaves COMMITTED=0, so the new slot boots as a trial. A slot left "not
# committed" by a refused bundle still boots if it is ever needed - as a
# trial, which it passes. The new trial also gets its own retry after a
# clean shutdown (BOAT_RETRY, see boat-commit).
set -eu
SELF=boat-rauc-pre-install
# shellcheck source=SCRIPTDIR/../../../../../recipes-boat/ab-boot/files/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh
# A warning for this install's history line (boat-rauc-post-install): RAUC
# shows a handler's output to nobody when the install succeeds.
INSTALL_WARNING="$BOAT_AB_RUNDIR/install-warning"
rm -f "$INSTALL_WARNING"
# The running slot's files (its board settings are compared below).
slot_root=/

slot="$(booted_slot)"
[ -n "$slot" ] || die "this boot did not come through GRUB (no rauc.slot=) - reboot first; installing now could leave nothing bootable"
mountpoint -q "$BOAT_AB_ESP_MOUNT" || die "the ESP is not mounted at $BOAT_AB_ESP_MOUNT"
check_layout || die "the disk does not match this image's A/B layout"
grubenv_valid "$BOAT_AB_GRUBENV" || die "grubenv is not valid - reboot so boat-grubenv-guard can rebuild it"
# On trial: booted as one and not committed yet this boot (boat-commit's
# mark), or not committed in grubenv - as boat-update decides it.
if booted_on_trial; then
    [ -e "$BOAT_AB_RUNDIR/committed" ] \
        || die "slot $slot is still on trial - let it commit (or roll back) before installing"
elif ! booted_lastresort; then
    [ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ] \
        || die "slot $slot is still on trial - let it commit (or roll back) before installing"
fi

# boat-update writes "VERSION PID" while it runs an install; a file whose
# writer is gone is left over and means nothing.
expect=/run/boat-update/expect
if [ -r "$expect" ]; then
    want_release="" want_pid=""
    read -r want_release want_pid < "$expect" || true
    if [ -n "$want_pid" ] && [ -d "/proc/$want_pid" ] \
            && [ "${RAUC_META_BOAT_RELEASE:-}" != "$want_release" ]; then
        die "this bundle is release '${RAUC_META_BOAT_RELEASE:-?}', but boat-update is installing $want_release - refusing it"
    fi
fi

# INI value: ini_get FILE SECTION KEY, blanks around it trimmed.
ini_get() {
    awk -v sec="[$2]" -v key="$3" '
        /^[ \t]*\[/ { s = $0; gsub(/[ \t\r]/, "", s); insec = (s == sec); next }
        insec {
            line = $0; sub(/^[ \t]+/, "", line)
            if (index(line, key) != 1) next
            rest = substr(line, length(key) + 1); sub(/^[ \t]+/, "", rest)
            if (substr(rest, 1, 1) != "=") next
            v = substr(rest, 2); sub(/^[ \t]+/, "", v); sub(/[ \t\r]+$/, "", v)
            print v; exit
        }' "$1"
}

# True when version $1 <= $2, by RAUC's own rules (r_semver_less_equal):
# MAJOR[.MINOR[.PATCH]] numerically, then no pre-release above any, then
# the pre-release's dot-separated fields - numbers numerically and below
# words, words byte by byte. Exit 2 when either does not parse.
semver_le() {
    LC_ALL=C awk -v a="$1" -v b="$2" '
        function parse(v, c,    n, p, i) {
            if (match(v, /\+[^+]*$/)) v = substr(v, 1, RSTART - 1)
            pre = ""; haspre = 0
            if (match(v, /-[^-]*$/)) { pre = substr(v, RSTART + 1); haspre = 1; v = substr(v, 1, RSTART - 1) }
            n = split(v, p, ".")
            if (n < 1 || n > 3) exit 2
            for (i = 1; i <= 3; i++) c[i] = 0
            for (i = 1; i <= n; i++) { if (p[i] !~ /^[0-9]+$/) exit 2; c[i] = p[i] + 0 }
        }
        function le() {
            parse(a, ca); pa = pre; ha = haspre
            parse(b, cb); pb = pre; hb = haspre
            for (i = 1; i <= 3; i++) {
                if (ca[i] < cb[i]) return 1
                if (ca[i] > cb[i]) return 0
            }
            if (!ha && !hb) return 1
            if (!ha) return 0
            if (!hb) return 1
            na = split(pa, fa, "."); nb = split(pb, fb, ".")
            for (i = 1; i <= na && i <= nb; i++) {
                xa = (fa[i] ~ /^[0-9]+$/); xb = (fb[i] ~ /^[0-9]+$/)
                if (xa && xb) {
                    if (fa[i] + 0 < fb[i] + 0) return 1
                    if (fa[i] + 0 > fb[i] + 0) return 0
                } else if (xa) return 1
                else if (xb) return 0
                else if (fa[i] < fb[i]) return 1
                else if (fa[i] > fb[i]) return 0
            }
            return na <= nb
        }
        BEGIN { exit !le() }'
}

# RAUC checks the bundle's compatible and min-bundle-version only after this
# handler has run: check them first, so that a bundle RAUC is going to
# refuse never demotes the fallback slot below.
manifest="${RAUC_BUNDLE_MOUNT_POINT:-}/manifest.raucm"
sys_conf="${RAUC_SYSTEM_CONFIG:-/etc/rauc/system.conf}"
[ -r "$manifest" ] || die "cannot read the bundle's manifest ($manifest)"
board_compat="$(ini_get "$sys_conf" system compatible)"
bundle_compat="$(ini_get "$manifest" update compatible)"
[ -n "$board_compat" ] || die "cannot read compatible= from $sys_conf"
[ "$bundle_compat" = "$board_compat" ] \
    || die "the bundle is for '$bundle_compat', this board is '$board_compat' - RAUC would refuse it"
min_version="$(ini_get "$sys_conf" system min-bundle-version)"
if [ -n "$min_version" ]; then
    bundle_version="$(ini_get "$manifest" update version)"
    [ -n "$bundle_version" ] || die "the bundle has no version, and this board takes nothing below $min_version"
    rc=0
    semver_le "$min_version" "$bundle_version" || rc=$?
    case "$rc" in
        0) ;;
        1) die "the bundle is version $bundle_version, below this board's min-bundle-version $min_version - RAUC would refuse it" ;;
        *) die "cannot compare the bundle's version '$bundle_version' with min-bundle-version '$min_version'" ;;
    esac
fi

data_dev="$(part_dev "$BOAT_AB_PART_DATA")"
awk -v d="$data_dev" '$1 == d && $2 == "/data" { f = 1 } END { exit !f }' /proc/mounts \
    || die "/data is not mounted from DATA ($data_dev)"
# RAUC marks the target unusable (OK=0) and only then saves its status to
# /data/rauc, by writing a new file and renaming it - and again once the
# slot is written. Without a free block or inode there it fails after the
# mark: the fallback slot is gone and nothing is installed. A streamed
# install needs no more room than this. df's figure leaves out the root
# reserve, which RAUC could use too: the check errs on the safe side.
data_kib="$(df -Pk /data 2>/dev/null | awk 'NR == 2 { print $4 }')"
# A file system that does not count inodes has none in all.
data_inodes="$(df -Pi /data 2>/dev/null | awk 'NR == 2 && $2 != 0 { print $4 }')"
case "$data_kib" in
    ''|*[!0-9]*) die "cannot read the free space on /data" ;;
esac
[ "$data_kib" -ge 8192 ] \
    || die "/data has $((data_kib / 1024)) MiB free - RAUC needs a few MiB there to save its status, and would mark slot(s) unusable first: free some space (docker system prune, journalctl --vacuum-size) and try again"
case "$data_inodes" in
    ''|*[!0-9]*) ;;
    *) [ "$data_inodes" -ge 64 ] \
           || die "/data has $data_inodes free inodes - RAUC needs some to save its status, and would mark slot(s) unusable first: remove files (docker system prune) and try again" ;;
esac

# The L4T release, as "35.6.4", from nv_tegra_release's first line
# ("# R35 (release), REVISION: 6.4, ...").
running_l4t="$(sed -n '1s/^# R\([0-9]*\) (release), REVISION: \([0-9]*\)\.\([0-9]*\).*/\1.\2.\3/p' /etc/nv_tegra_release 2>/dev/null)"
bundle_l4t="${RAUC_META_BOAT_L4T:-}"
[ -n "$bundle_l4t" ] || die "the bundle has no [meta.boat] l4t= - it was not built by meta-boat's boat-bundle"
[ -n "$running_l4t" ] || die "cannot read the running L4T release from /etc/nv_tegra_release"
if [ "$bundle_l4t" != "$running_l4t" ]; then
    die "the bundle is for L4T $bundle_l4t, this board runs $running_l4t: a BSP change also changes firmware that only a USB reflash updates (scripts/05-flash-nvme.sh)"
fi
# The kernel runs on the device tree written at flash; a slot carries none.
# Each image records the DTB and overlays it was built against, and every
# install checks it against the running slot's - which was flashed with
# them, or checked the same way - so a release built against another DTB
# (a meta-tegra or kernel change within one L4T release) is refused.
# Boards flashed before the record existed cannot check it.
running_fw="$(cat /usr/lib/boat-ab/firmware.id 2>/dev/null || true)"
bundle_fw="${RAUC_META_BOAT_FIRMWARE:-}"
if [ -n "$running_fw" ] && [ -n "$bundle_fw" ] && [ "$bundle_fw" != "$running_fw" ]; then
    die "the bundle was built against another kernel device tree (firmware.id $bundle_fw, this slot's $running_fw): a DTB change only reaches the board by a USB reflash at the dock (scripts/05-flash-nvme.sh) - publish such a release with --reflash: boat-update then tells boards flashed with the old one to reflash instead of offering it"
elif [ -z "$running_fw" ] || [ -z "$bundle_fw" ]; then
    warn "cannot check the kernel device tree: $([ -z "$running_fw" ] && echo "this slot" || echo "the bundle") has no firmware.id (built before it was recorded)"
fi

# Docker keeps its data on the shared /data: a newer major version may
# migrate it in a way the other slot's Docker cannot read after a fallback.
running_docker="$(docker --version 2>/dev/null | sed -n 's/^Docker version \([0-9]*\)\..*/\1/p')"
bundle_docker="${RAUC_META_BOAT_DOCKER:-}"
if [ -n "$running_docker" ] && [ -n "$bundle_docker" ] && [ "${bundle_docker%%.*}" != "$running_docker" ]; then
    # On a successful install RAUC shows this output to nobody: it goes to
    # 'boat-update status' (an alert, until the reboot) and the history.
    docker_warning="Docker $running_docker -> ${bundle_docker}: a new major version may migrate /data/docker so that this slot's Docker cannot read it after a fallback or rollback"
    alert "$docker_warning"
    { mkdir -p "$BOAT_AB_RUNDIR" && echo "$docker_warning" > "$INSTALL_WARNING"; } 2>/dev/null || true
fi

targets=""
for n in ${RAUC_TARGET_SLOTS:-}; do
    # RAUC numbers the slots; their details are in RAUC_SLOT_*_<n>.
    case "$n" in ''|*[!0-9]*) die "unexpected RAUC_TARGET_SLOTS entry '$n'" ;; esac
    bootname="" device=""
    eval "bootname=\${RAUC_SLOT_BOOTNAME_$n:-}"
    eval "device=\${RAUC_SLOT_DEVICE_$n:-}"
    case "$bootname" in
        A) num="$BOAT_AB_PART_A" name=APP ;;
        B) num="$BOAT_AB_PART_B" name=APP_b ;;
        *) die "target slot $n has bootname '$bootname' - expected A or B" ;;
    esac
    [ "$bootname" != "$slot" ] || die "RAUC would write slot $bootname, which is the one running"
    [ "$device" = "$(part_dev "$num")" ] || die "slot $bootname is configured as $device, expected $(part_dev "$num")"
    check_part "$num" "$name" || die "not writing $device"
    targets="$targets $bootname"
done
[ -n "$targets" ] || die "RAUC named no target slot"

# Board settings live in each slot and are carried only at an install, from
# the running slot. After a rollback or a fallback the target slot can hold
# newer ones - a Wi-Fi network or a pairing added since - and overwriting it
# would lose them for good. Keep what differs, and say so. Best effort: a
# slot that will not mount (never written, half written) has nothing to keep.
#
# Only what changed there after the later of the two slots' installs counts.
# On a forward update the target is the older slot: its settings are the
# ones this slot was installed from, or older - stale copies (BlueZ
# rewrites a pairing's info by itself), not displaced ones. After a
# rollback the target was installed later, and what changed there since
# never reached this slot. The times are RAUC's installed.timestamp from its
# central status file: every RAUC install records it, also those made by
# older bundles (a marker the bundle hook wrote would not be in a slot an
# older bundle installed), and 'rauc status' cannot be asked while RAUC runs
# this install. Neither slot recorded (a reflash empties /data and writes
# only APP): the target holds what was there before the flash - stale.
# The times are only as good as the clock was then: a file changed while
# the clock read 1970 looks old, and an install made then makes every file
# look new (kept, as before).
rauc_status="$(ini_get "$sys_conf" system data-directory 2>/dev/null || true)"
rauc_status="${rauc_status:-/data/rauc}/central.raucs"
# installed_at BOOTNAME: when RAUC last installed that slot, in seconds
# since the epoch; nothing if it has no record. system.conf's rootfs.0 is A.
installed_at() {
    case "$1" in A) name=rootfs.0 ;; *) name=rootfs.1 ;; esac
    ts="$(ini_get "$rauc_status" "slot.$name" installed.timestamp 2>/dev/null || true)"
    [ -n "$ts" ] || return 0
    # RAUC writes 2026-10-16T12:00:00Z; busybox's date wants a space for the T.
    ts="$(echo "$ts" | sed 's/T/ /; s/\.[0-9]*//; s/Z$//')"
    date -u -d "$ts" +%s 2>/dev/null || true
}
keep_displaced() { # BOOTNAME DEVICE
    since="$(installed_at "$slot")"
    target_since="$(installed_at "$1")"
    if [ -z "$since" ] || { [ -n "$target_since" ] && [ "$target_since" -gt "$since" ]; }; then
        since="$target_since"
    fi
    [ -n "$since" ] || return 0
    mnt="$BOAT_AB_RUNDIR/displaced-$1"
    mkdir -p "$mnt" || return 0
    # Read only, and nothing on it can run or open a device.
    if ! mount -o ro,noload,nosuid,nodev,noexec -t ext4 "$2" "$mnt" 2>/dev/null; then
        rmdir "$mnt" 2>/dev/null || true
        return 0
    fi
    # Network connections, Bluetooth pairings and root's authorized keys:
    # what someone set up, not what changes by itself (caches, known_hosts).
    # find fails for a directory the slot does not have; the rest is listed.
    ( cd "$mnt" || exit 0
      find etc/NetworkManager/system-connections -type f 2>/dev/null || true
      find var/lib/bluetooth -type f -name info 2>/dev/null || true
      find home/root/.ssh -type f -name authorized_keys 2>/dev/null || true ) > "$mnt.list"
    keep="/data/boat-update/displaced/slot-$1-$(date -u +%Y%m%dT%H%M%SZ)"
    found=""
    # Every file there changed after that, which this slot lacks or has
    # with other contents.
    while IFS= read -r f; do
        [ "$(stat -c %Y "$mnt/$f" 2>/dev/null || echo 0)" -gt "$since" ] || continue
        [ -f "$slot_root$f" ] \
            && [ "$(sha256sum < "$mnt/$f")" = "$(sha256sum < "$slot_root$f")" ] && continue
        (umask 077 && mkdir -p "$keep/$(dirname "$f")") && cp -a "$mnt/$f" "$keep/$f" \
            && found="$found /$f"
    done < "$mnt.list"
    rm -f "$mnt.list"
    # RAUC must not write a slot that is still mounted.
    umount "$mnt" || die "could not unmount slot $1 ($mnt) after reading its settings - nothing changed"
    rmdir "$mnt" 2>/dev/null || true
    if [ -n "$found" ]; then
        alert "slot $1 holds board settings this slot lacks or has otherwise (changed there after $(date -u -d "@$since" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "its install")):$found - kept in $keep before it is overwritten; copy back what is still wanted"
    fi
    return 0
}
for t in $targets; do
    case "$t" in A) num="$BOAT_AB_PART_A" ;; *) num="$BOAT_AB_PART_B" ;; esac
    keep_displaced "$t" "$(part_dev "$num")"
done

ab_lock || die "could not take the A/B lock - another grubenv writer is stuck; nothing changed"
for t in $targets; do
    grubenv_set "${t}_COMMITTED=0" BOAT_RETRY=
    say "slot $t: marked not committed - it boots only as a trial until one passes"
done
ab_unlock
# System handlers get no RAUC_MF_*: the version comes from [meta.boat].
say "installing ${RAUC_META_BOAT_RELEASE:-?} into slot(s)$targets"
