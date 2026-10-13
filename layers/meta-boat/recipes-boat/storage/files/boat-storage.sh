#!/bin/sh
# boat-storage: the A/B layout's shared partitions - SWAP and DATA - and the
# growth of the flashed filesystems into their partitions. See
# docs/plans/ab-os-updates.md sections 5.2 and 5.8.
#
#   boat-storage status             what SWAP, DATA and both slots hold
#   boat-storage format-data [--yes]
#                                   make a NEW, EMPTY filesystem on DATA -
#                                   destroys everything on it; asks first
#
# Run at boot by systemd units this package ships:
#   boat-storage swap               mkswap SWAP unless it holds swap, swapon
#   boat-storage check-data         vet DATA before data.mount mounts it
#   boat-storage prepare-data       /data's directories, seed the user homes
#   boat-storage grow               grow DATA and this slot's ext4 into their
#                                   partitions (online; the table never changes)
#
# Nothing here formats DATA on its own. The flash writes DATA's first
# filesystem (boat-data-seed, a small ext4 the first boot grows), and after
# that the only way to a new one is `format-data`, typed by a person. A DATA
# that does not check out is left exactly as it is and not mounted: the slot
# still boots, just without /data, and `boat-storage status` says why.
#
# Every partition is addressed as <disk>p<N> and its GPT name is checked
# before anything touches it, so a foreign or re-partitioned drive is never
# written to (plan section 5.2).
set -eu
SELF=boat-storage
# shellcheck source=SCRIPTDIR/../../ab-boot/files/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

DATA_LABEL=boat-data
DATA_MOUNT=/data
# Users whose home lives on DATA, so it survives an update. root's home
# (/home/root on this distro) deliberately stays in the slot: it must still
# hold the SSH keys when DATA is the thing that broke.
HOME_USERS="@HOME_USERS@"

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

fs_type() { blkid -p -o value -s TYPE "$1" 2>/dev/null || true; }
fs_label() { blkid -p -o value -s LABEL "$1" 2>/dev/null || true; }

# The device the filesystem visible at mount point $1 came from, or nothing.
mount_source() {
    awk -v m="$1" '$2 == m { s = $1 } END { print s }' /proc/mounts
}

is_mounted_dev() {
    awk -v d="$1" '$1 == d { f = 1 } END { exit !f }' /proc/mounts
}

is_swap_dev() {
    awk -v d="$1" '$1 == d { f = 1 } END { exit !f }' /proc/swaps
}

cmd_swap() {
    dev="$(part_dev "$BOAT_AB_PART_SWAP")"
    wait_dev "$dev" 10 || die "$dev did not appear - running without swap"
    check_part "$BOAT_AB_PART_SWAP" SWAP || die "not touching $dev - running without swap"
    if is_swap_dev "$dev"; then
        say "$dev is already in use as swap"
        return 0
    fi
    type="$(fs_type "$dev")"
    case "$type" in
        swap) ;;
        swsuspend)
            die "$dev holds a hibernation image - leaving it alone (resume from it, or run 'mkswap $dev' to discard it)" ;;
        *)
            # Swap keeps nothing from one boot to the next, so whatever is
            # here - nothing after a flash, or an older layout's leftovers -
            # can go.
            say "$dev holds ${type:-no signature} - making it swap"
            mkswap -L boat-swap "$dev" >/dev/null ;;
    esac
    swapon "$dev"
    say "swap enabled on $dev"
}

cmd_check_data() {
    dev="$(part_dev "$BOAT_AB_PART_DATA")"
    wait_dev "$dev" 10 || die "$dev did not appear - /data will not be mounted"
    check_part "$BOAT_AB_PART_DATA" DATA || die "not mounting $dev"
    type="$(fs_type "$dev")"
    label="$(fs_label "$dev")"
    if [ "$type" != ext4 ] || [ "$label" != "$DATA_LABEL" ]; then
        die "$dev holds ${type:-no filesystem}${label:+ labelled '$label'}, not the ext4 '$DATA_LABEL' the flash puts there - not mounting it. 'boat-storage status' shows more; 'boat-storage format-data' makes a new, empty one."
    fi
    # Preen: repair what is safe to repair unattended, and refuse the rest
    # rather than mount a damaged filesystem every slot shares.
    rc=0
    e2fsck -p "$dev" || rc=$?
    case "$rc" in
        0|1|2) ;;
        *) die "'e2fsck -p $dev' exited $rc: problems it will not fix unattended - not mounting it. Run 'e2fsck $dev' by hand." ;;
    esac
}

cmd_prepare_data() {
    [ "$(mount_source "$DATA_MOUNT")" = "$(part_dev "$BOAT_AB_PART_DATA")" ] \
        || die "$DATA_MOUNT is not mounted from DATA"
    mkdir -p "$DATA_MOUNT/home" "$DATA_MOUNT/log/journal"
    # A home is copied from the image once, the first time DATA is seen
    # without it; after that the copy on DATA is the user's home in every
    # slot. Copied before the bind mounts in this package hide the image's
    # own /home/<user>, which is why this runs before them.
    for user in $HOME_USERS; do
        src="/home/$user"
        dst="$DATA_MOUNT/home/$user"
        [ -e "$dst" ] && continue
        if [ ! -d "$src" ]; then
            warn "this image has no $src - not seeding $dst"
            continue
        fi
        if mountpoint -q "$src"; then
            die "$src is already a mount point - not seeding $dst from it"
        fi
        rm -rf "$dst.seeding"
        cp -a "$src" "$dst.seeding"
        # On disk before the rename makes it final: a power cut must not
        # leave a complete-looking home of empty files, never seeded again.
        sync -f "$DATA_MOUNT"
        mv "$dst.seeding" "$dst"
        sync -f "$DATA_MOUNT"
        say "seeded $dst from the image's $src"
    done
}

# Grow the ext4 on $1, mounted at $2, to the size of its partition. Online
# only, and only when $2 really is mounted from $1.
grow_fs() {
    if [ "$(mount_source "$2")" != "$1" ]; then
        say "$2 is not mounted from $1 - not growing it"
        return 0
    fi
    out="$(resize2fs "$1" 2>&1)" || {
        warn "resize2fs $1 failed:"
        printf '%s\n' "$out" >&2
        return 1
    }
    case "$out" in
        *"Nothing to do"*) ;;
        *) say "grew the filesystem on $1 ($2) to fill its partition" ;;
    esac
}

cmd_grow() {
    rc=0
    if check_part "$BOAT_AB_PART_DATA" DATA; then
        grow_fs "$(part_dev "$BOAT_AB_PART_DATA")" "$DATA_MOUNT" || rc=1
    fi
    slot="$(booted_slot)"
    case "$slot" in
        A) num="$BOAT_AB_PART_A" name=APP ;;
        B) num="$BOAT_AB_PART_B" name=APP_b ;;
        *)
            say "booted outside GRUB (no rauc.slot=) - not growing the root filesystem"
            return $rc ;;
    esac
    if check_part "$num" "$name"; then
        grow_fs "$(part_dev "$num")" / || rc=1
    fi
    return $rc
}

# One line per partition: device, GPT name, what it holds, how it is used.
describe() {
    dev="$(part_dev "$1")"
    if [ ! -b "$dev" ]; then
        printf '  %-8s %-16s missing\n' "$2" "$dev"
        return 0
    fi
    name="$(part_name "$1" || true)"
    type="$(fs_type "$dev")"
    label="$(fs_label "$dev")"
    use=""
    if is_swap_dev "$dev"; then
        use="in use as swap"
    elif is_mounted_dev "$dev"; then
        use="mounted on $(awk -v d="$dev" '$1 == d { print $2; exit }' /proc/mounts)"
    fi
    printf '  %-8s %-16s GPT "%s"  %s%s  %s\n' "$2" "$dev" "${name:-?}" \
        "${type:-no filesystem}" "${label:+ \"$label\"}" "$use"
    if [ "$name" != "$2" ]; then
        printf '           ^ expected GPT name "%s" - nothing acts on this partition\n' "$2"
    fi
}

cmd_status() {
    echo "A/B storage on ${BOAT_AB_DISK}:"
    describe "$BOAT_AB_PART_A" APP
    describe "$BOAT_AB_PART_B" APP_b
    describe "$BOAT_AB_PART_SWAP" SWAP
    describe "$BOAT_AB_PART_DATA" DATA
    if [ "$(mount_source "$DATA_MOUNT")" = "$(part_dev "$BOAT_AB_PART_DATA")" ]; then
        df -h "$DATA_MOUNT" | sed 's/^/  /'
    else
        echo "  /data is NOT mounted from DATA - see: journalctl -b -u boat-data-check -u data.mount"
    fi
}

cmd_format_data() {
    dev="$(part_dev "$BOAT_AB_PART_DATA")"
    [ -b "$dev" ] || die "$dev does not exist"
    check_part "$BOAT_AB_PART_DATA" DATA || die "refusing to format $dev"
    if is_mounted_dev "$dev"; then
        die "$dev is mounted - stop what uses /data (docker, boat-compose) and 'umount /data' first, or boot with it unmounted"
    fi
    type="$(fs_type "$dev")"
    label="$(fs_label "$dev")"
    echo "This makes a NEW, EMPTY filesystem on $dev (the DATA partition)."
    echo "Everything on it is destroyed: Docker images, containers and volumes,"
    echo "the compose files, the homes kept on /data, and the saved logs."
    if [ -n "$type" ]; then
        echo "It currently holds: $type${label:+ \"$label\"}"
    fi
    if [ "${1:-}" != "--yes" ]; then
        printf 'Type "format DATA" to go ahead: '
        read -r answer || answer=""
        [ "$answer" = "format DATA" ] || die "not confirmed - nothing was changed"
    fi
    mkfs.ext4 -F -q -b 4096 -m 1 -L "$DATA_LABEL" "$dev"
    say "made a new filesystem on $dev. Reboot, or 'systemctl start data.mount', to use it."
}

[ "$#" -ge 1 ] || usage 2
cmd="$1"
shift
case "$cmd" in
    swap) cmd_swap ;;
    check-data) cmd_check_data ;;
    prepare-data) cmd_prepare_data ;;
    grow) cmd_grow ;;
    status) cmd_status ;;
    format-data) cmd_format_data "$@" ;;
    -h|--help|help) usage 0 ;;
    *) usage 2 ;;
esac
