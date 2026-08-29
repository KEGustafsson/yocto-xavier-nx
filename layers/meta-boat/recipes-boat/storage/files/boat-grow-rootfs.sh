#!/bin/sh
# Provision swap, then grow the boat computer's root filesystem to fill the
# rest of the NVMe SSD.
#
#   boat-grow-rootfs                 # report what would change, touch nothing
#   boat-grow-rootfs --grow          # do it (asks for confirmation first)
#   boat-grow-rootfs --grow --yes    # do it without the prompt
#   boat-grow-rootfs --dry-run       # print the exact commands, run none
#   boat-grow-rootfs --grow --no-swap        # rootfs only, as this used to be
#   boat-grow-rootfs --grow --swap-size 16G  # a different amount of swap
#
# Run it from a terminal on the XFCE desktop (or over SSH) after the first
# boot:  sudo boat-grow-rootfs        then     sudo boat-grow-rootfs --grow
#
# WHY THIS IS NEEDED
# The image is flashed with a fixed-size root filesystem - ROOTFS_SIZE_BYTES
# in scripts/env.sh, 16 GiB by default, kept small because make-sdcard writes
# every byte of it over USB 2.0 whether it holds data or not - and
# NVIDIA's initrd-flash writes a
# GPT that describes only that much of the drive. On any SSD bigger than
# that, everything past the APP partition is simply unallocated, and the
# GPT's backup header sits in the middle of the disk instead of at its end.
# Nothing on the host side can know the SSD's size at build time, so the
# reclaim has to happen once, on the device. The image also ships with no
# swap of any kind - no partition, no swapfile, no zram - so this is where
# swap gets provisioned too.
#
# TWO DIFFERENT GAPS, AND THEY ARE NOT THE SAME
# CONFIRMED ON HARDWARE: after a normal flash the APP *partition* already
# fills the whole SSD - make-sdcard creates the last partition with a
# "fill to end" flag, sizing it to the physical drive rather than to
# ROOTFS_SIZE_BYTES. What is undersized is the *filesystem inside it*: a
# 232 GiB partition holding a 16 GiB ext4. So the usual job here is a plain
# resize2fs, with no partition table change at all.
#
# The other gap - unallocated space after the partition - only happens when
# the partition really is short (a hand-made layout, a restored image). This
# script handles both and reports them separately, so it is obvious which
# one you have.
#
# SWAP GOES AT THE TAIL, AND THE ROOTFS STOPS SHORT OF IT
# APP is partition 1 but it is the LAST partition on the disk by position -
# everything before it (kernel, kernel-dtb, the A/B chain reserves, recovery,
# RECROOTFS, esp, esp_alt, UDA) sits below its start sector. So the only
# place a swap partition can go is the tail, and provisioning it is one
# operation rather than two: set APP to end exactly where swap begins, then
# create swap in the space left over. That single "set APP's size" step is a
# GROW when the partition was short and a SHRINK when it already filled the
# disk, which is why there is no separate code path for the two cases.
#
# The shrink is only safe while the ext4 inside is still smaller than the
# target - which is exactly the post-flash state, a 16 GiB filesystem in a
# 232 GiB partition. ext4 cannot shrink online, so once the filesystem has
# been grown to fill the disk that door is closed: from then on the honest
# answer is a swapfile, and that is what this falls back to. Run it once,
# early, and you get the partition.
#
# CONFIRMED ON HARDWARE (loop device, same util-linux/gptfdisk/e2fsprogs as
# the image): sfdisk shrinks a MOUNTED partition's table entry and returns 0,
# sgdisk then carves the tail, `partx -u` shrinks the kernel's view and
# `partx -a` adds the new one, mkswap/swapon take, resize2fs grows the still-
# mounted ext4 into what is left, and a canary file written beforehand is
# intact afterwards. The kernel permits the shrink because BLKPG_RESIZE_
# PARTITION only refuses a changed start sector or an overlap, neither of
# which this does - but that is also why the filesystem-fits check below is
# not optional: nothing in the kernel or in sfdisk will stop you shrinking a
# partition out from under a filesystem that no longer fits.
#
# WHY IT IS SAFE TO RUN ON THE RUNNING SYSTEM
# ext4 supports online resize, so / stays mounted throughout and no file data
# is rewritten. Growing the rootfs partition only ever claims space that is
# unallocated - the free-space figure below is measured up to the next
# partition, not to the end of the disk, so a /data or swap partition sitting
# behind the rootfs bounds the grow instead of being overrun. No partition is
# ever moved.
#
# IT COMPETES WITH A SEPARATE /data PARTITION, AND ONLY ONE OF YOU CAN WIN
# --grow extends the rootfs to the last usable sector (or to the swap
# partition it just made), so afterwards there is no unallocated space left
# to carve /data out of - and /data is where daemon.json points Docker's
# data-root and where boat-compose looks for the operator's stack. If you
# want /data on its own partition, create it FIRST: the rootfs then grows
# only into the gap in front of it, which is the half that matters after a
# normal flash anyway. See docs/05-phase2-boat-computer-layer.md "Reclaiming
# the rest of the SSD".
#
# If power is lost between the partition edit and resize2fs, just run it
# again - it picks up wherever it stopped.
set -eu

SELF=boat-grow-rootfs

MODE=status          # status | dryrun | grow
ASSUME_YES=0

# 8 GiB of swap on a 6.8 GiB-RAM Xavier NX: enough to absorb a container
# build or a model load that would otherwise be OOM-killed, without giving
# the box so much that it thrashes instead of failing. Override per-run with
# --swap-size, or set it to 0 (or pass --no-swap) to skip swap entirely.
SWAP_BYTES=$(( 8 * 1024 * 1024 * 1024 ))
SWAPFILE=/swapfile
SWAP_PART_LABEL=boat-swap

say() { echo "${SELF}: $*"; }
die() { echo "${SELF}: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: boat-grow-rootfs [options]

Provision swap, then grow the root filesystem - and the partition holding it
- to fill the rest of the NVMe SSD. The image ships a deliberately small
16 GiB rootfs (it is flashed byte-for-byte over USB 2.0) and no swap at all.

  -s, --status         report current and reclaimable space, change nothing
                       (the default)
  -n, --dry-run        run every check and print the exact commands, run none
  -g, --grow           actually provision swap, grow the partition and the
                       filesystem
  -y, --yes            skip the confirmation prompt (implies nothing on its own)
      --swap-size SIZE how much swap to provision (default 8G; 0 disables).
                       Accepts a plain byte count or a K/M/G/T suffix.
      --no-swap        same as --swap-size 0
  -h, --help           this text

Needs root. Safe while / is mounted: the rootfs partition is the last one on
the disk, only unallocated space is claimed, and ext4 is resized online.

Swap is provisioned as a PARTITION at the end of the disk when that is still
possible - which means before the rootfs filesystem has been grown, i.e. the
first time this is run on a freshly flashed board. Once the filesystem fills
the disk it can no longer be shrunk online, and swap falls back to a file at
/swapfile. Either way an entry is added to /etc/fstab and swap is enabled
immediately.
USAGE
    exit "${1:-0}"
}

# Accepts "8G", "8192M", "0", or a plain byte count. Deliberately strict: a
# typo here would otherwise be read as a byte count and silently produce a
# swap area of a few bytes, or - worse, in the partition path - a nonsense
# target size for the rootfs partition.
parse_size() {
    echo "$1" | awk '
        BEGIN { mult["K"]=1024; mult["M"]=1048576
                mult["G"]=1073741824; mult["T"]=1099511627776 }
        {
            s = toupper($0)
            if (!match(s, /^[0-9]+/)) { print "BAD"; exit }
            n = substr(s, 1, RLENGTH) + 0
            rest = substr(s, RLENGTH + 1)
            if (rest == "") { printf "%d", n; exit }   # a plain byte count
            u = substr(rest, 1, 1)
            # Accept G, GB, GiB and GI - and nothing else. Matching the unit
            # letter alone is not enough: an earlier version keyed the
            # multiplier off the LAST character, so "8Gi" looked up mult["I"],
            # got the empty string, and quietly returned 0 - which reads as
            # --no-swap rather than as the typo it is.
            if ((u in mult) && (rest == u || rest == u "B" || rest == u "I" || rest == u "IB")) {
                printf "%d", n * mult[u]
                exit
            }
            print "BAD"
        }'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -s|--status)  MODE=status ;;
        -n|--dry-run) MODE=dryrun ;;
        -g|--grow)    MODE=grow ;;
        -y|--yes)     ASSUME_YES=1 ;;
        --no-swap)    SWAP_BYTES=0 ;;
        --swap-size)
            [ "$#" -ge 2 ] || die "--swap-size needs a value (e.g. --swap-size 8G)"
            SWAP_BYTES="$(parse_size "$2")"
            [ "$SWAP_BYTES" = "BAD" ] && die "cannot parse swap size '$2' - try 8G, 8192M or a byte count"
            shift ;;
        --swap-size=*)
            SWAP_BYTES="$(parse_size "${1#--swap-size=}")"
            [ "$SWAP_BYTES" = "BAD" ] && die "cannot parse swap size '${1#--swap-size=}' - try 8G, 8192M or a byte count" ;;
        -h|--help)    usage 0 ;;
        *)            echo "${SELF}: unknown argument '$1'" >&2; usage 1 ;;
    esac
    shift
done

# Human-readable sizes. Sectors are always 512 bytes in sysfs, whatever the
# drive's own logical block size is - that is a kernel ABI guarantee, so the
# arithmetic below can hardcode it.
SECTOR=512
human() {
    # $1 = sectors
    awk -v s="$1" -v b="$SECTOR" 'BEGIN {
        v = s * b
        split("B KiB MiB GiB TiB", u, " ")
        i = 1
        while (v >= 1024 && i < 5) { v /= 1024; i++ }
        printf "%.1f %s", v, u[i]
    }'
}

need() {
    command -v "$1" >/dev/null 2>&1 \
        || die "'$1' not found - install $2 (it is in packagegroup-boat-tools)"
}

# --- Work out what we are growing ------------------------------------------
# findmnt resolves whatever / is mounted from (a PARTUUID= or LABEL= root=
# on the kernel command line included) down to a real device node.
command -v findmnt >/dev/null 2>&1 || die "'findmnt' not found (util-linux-findmnt)"
ROOT_SRC="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
ROOT_FS="$(findmnt -n -o FSTYPE / 2>/dev/null || true)"
[ -n "$ROOT_SRC" ] || die "cannot determine the device / is mounted from"
[ -b "$ROOT_SRC" ] || die "/ is mounted from '$ROOT_SRC', which is not a block device"

case "$ROOT_FS" in
    ext2|ext3|ext4) ;;
    *) die "/ is $ROOT_FS; only ext2/3/4 can be grown by this script" ;;
esac

PART_KNAME="${ROOT_SRC#/dev/}"
[ -e "/sys/class/block/${PART_KNAME}/partition" ] \
    || die "$ROOT_SRC is not a partition (LVM? device-mapper?) - not handled"
PART_NUM="$(cat "/sys/class/block/${PART_KNAME}/partition")"
DISK_KNAME="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null | head -n1)"
[ -n "$DISK_KNAME" ] || die "cannot determine the disk holding $ROOT_SRC"
DISK="/dev/${DISK_KNAME}"
[ -b "$DISK" ] || die "$DISK is not a block device"

# lsblk first: it answers from udev, so it can work unprivileged. blkid -p
# probes the device itself and needs root, so it is only the fallback -
# meaning that on a system where udev has not recorded PTTYPE (CONFIRMED ON
# HARDWARE: this image is one), a non-root run gets nothing from either.
# Distinguish that from a genuinely non-GPT disk: reporting "has an 'unknown'
# partition table" blames the disk for what is really a missing sudo.
PT_TYPE="$(lsblk -dno PTTYPE "$DISK" 2>/dev/null | head -n1 || true)"
[ -n "$PT_TYPE" ] || PT_TYPE="$(blkid -p -o value -s PTTYPE "$DISK" 2>/dev/null || true)"
if [ -z "$PT_TYPE" ] && [ "$(id -u)" -ne 0 ]; then
    die "cannot read the partition table on ${DISK} without root - try: sudo ${SELF}"
fi
[ "$PT_TYPE" = "gpt" ] \
    || die "$DISK has a '${PT_TYPE:-unknown}' partition table, not GPT - not handled"

DISK_SECTORS="$(cat "/sys/class/block/${DISK_KNAME}/size")"
PART_START="$(cat "/sys/class/block/${PART_KNAME}/start")"
PART_SECTORS="$(cat "/sys/class/block/${PART_KNAME}/size")"
PART_END=$(( PART_START + PART_SECTORS - 1 ))

# GPT keeps its backup header plus the 128-entry array in the last 33
# sectors, so that is the highest sector a partition may ever end on.
GPT_TAIL=33
LAST_USABLE=$(( DISK_SECTORS - GPT_TAIL - 1 ))

# --- What else is on this disk, and where does the rootfs run out of room? --
# Iterate the disk's OWN children under /sys/block/<disk>/, not a prefix glob
# over every block device: /sys/class/block/sda* also matches sdaa1, sdab1...
# and /sys/class/block/nvme0n1* also matches nvme0n10p1 - partitions of
# completely unrelated drives. It fails safe (a spurious neighbour only ever
# makes the script claim less) but it would hold the grow short, on a real
# boat with a large enclosure attached, for a reason that is not true.
#
# NEXT_START is the first sector after the rootfs that something else already
# owns. The free space that matters is the gap up to THERE, not up to the end
# of the disk - an earlier version measured to the end, which meant that once
# this script had created the swap partition at the tail, its own next run
# saw 8 GiB of "reclaimable" space, found a partition in the way, and refused
# with "blocked" on a disk that was in fact exactly as intended.
NEXT_START=""
NEIGHBOURS=""
SWAP_PART=""
for pfile in /sys/block/"${DISK_KNAME}"/*/partition; do
    [ -e "$pfile" ] || continue
    pdir="$(dirname "$pfile")"
    pname="$(basename "$pdir")"
    [ "$pname" = "$PART_KNAME" ] && continue
    pstart="$(cat "${pdir}/start")"
    ptype="$(lsblk -no FSTYPE "/dev/${pname}" 2>/dev/null | head -n1 || true)"
    [ -n "$ptype" ] || ptype="$(blkid -o value -s TYPE "/dev/${pname}" 2>/dev/null || true)"
    [ "$ptype" = "swap" ] && SWAP_PART="/dev/${pname}"
    if [ "$pstart" -gt "$PART_END" ]; then
        NEIGHBOURS="${NEIGHBOURS} ${pname}(starts at sector ${pstart})"
        if [ -z "$NEXT_START" ] || [ "$pstart" -lt "$NEXT_START" ]; then
            NEXT_START="$pstart"
        fi
    fi
done

if [ -n "$NEXT_START" ]; then
    ROOT_CEILING=$(( NEXT_START - 1 ))
else
    ROOT_CEILING="$LAST_USABLE"
fi
FREE_SECTORS=$(( ROOT_CEILING - PART_END ))
[ "$FREE_SECTORS" -ge 0 ] || FREE_SECTORS=0

# --- How big is the filesystem itself? -------------------------------------
# This is the gap that actually matters after a flash, and the one an earlier
# version of this script missed entirely: it only looked for unallocated
# space after the partition, found none (make-sdcard had already sized the
# partition to the whole drive), and reported "nothing to do" while the ext4
# inside sat at its flashed size.
#
# dumpe2fs is exact but has to read the device, so it needs root; df is the
# unprivileged fallback and understates a little (it excludes metadata and
# reserved blocks). For the grow that skew is harmless - at worst it offers a
# resize2fs that turns out to be a no-op, never the reverse. For the swap
# SHRINK it would not be, so FS_EXACT gates that path below: an understated
# filesystem size is exactly how you would talk yourself into shrinking a
# partition that the filesystem no longer fits inside.
FS_SECTORS=0
FS_EXACT=0
if command -v dumpe2fs >/dev/null 2>&1; then
    FS_INFO="$(dumpe2fs -h "$ROOT_SRC" 2>/dev/null || true)"
    FS_BLOCKS="$(echo "$FS_INFO" | awk -F: '/^Block count:/ {gsub(/ /,"",$2); print $2}')"
    FS_BSIZE="$(echo "$FS_INFO" | awk -F: '/^Block size:/ {gsub(/ /,"",$2); print $2}')"
    if [ -n "$FS_BLOCKS" ] && [ -n "$FS_BSIZE" ]; then
        FS_SECTORS=$(( FS_BLOCKS * (FS_BSIZE / SECTOR) ))
        FS_EXACT=1
    fi
fi
if [ "$FS_EXACT" -eq 0 ]; then
    FS_1K="$(df -Pk / 2>/dev/null | awk 'NR==2 {print $2}')"
    [ -n "$FS_1K" ] || FS_1K=0
    FS_SECTORS=$(( FS_1K * 2 ))
fi

# --- Is there swap already? ------------------------------------------------
# Any of the three counts: an active swap area (whatever backs it), a swap
# partition on this disk that simply is not switched on, or the swapfile this
# script would otherwise create. Re-running must never end up with two.
SWAP_ACTIVE_SECTORS=0
if [ -r /proc/swaps ]; then
    SWAP_ACTIVE_SECTORS="$(awk 'NR > 1 {t += $3} END {printf "%d", t * 2}' /proc/swaps)"
fi
SWAP_EXISTS=0
[ "$SWAP_ACTIVE_SECTORS" -gt 0 ] && SWAP_EXISTS=1
[ -n "$SWAP_PART" ] && SWAP_EXISTS=1
[ -e "$SWAPFILE" ] && SWAP_EXISTS=1

# --- Plan the swap partition, if one is wanted and still possible ----------
# Align to 2048 sectors (1 MiB) like every other partition on this table.
# Rounding the START down rather than the size up keeps swap at least the
# requested size and keeps the rootfs a whisker smaller, which is the right
# way round: the rootfs is the thing that can absorb it.
SWAP_SECTORS=$(( SWAP_BYTES / SECTOR ))
# Rounded UP: dd counts whole MiB, so truncating turns --swap-size 512K into
# count=0 and an empty file that mkswap then refuses. Any positive request
# should yield at least 1 MiB.
SWAP_MB=$(( (SWAP_BYTES + 1048576 - 1) / 1048576 ))
SWAP_START=$(( (LAST_USABLE - SWAP_SECTORS + 1) / 2048 * 2048 ))
SWAP_END="$LAST_USABLE"
ROOT_TARGET_END=$(( SWAP_START - 1 ))
ROOT_TARGET_SECTORS=$(( ROOT_TARGET_END - PART_START + 1 ))

# Leave the filesystem a little room below the partition edge rather than
# shrinking to exactly its size - resize2fs is not the only thing that reads
# the tail of a partition, and a 64 MiB cushion costs nothing at these sizes.
SHRINK_MARGIN=$(( 64 * 1024 * 1024 / SECTOR ))

SWAP_MODE="none"     # none | partition | file
SWAP_WHY=""
if [ "$SWAP_BYTES" -gt 0 ] && [ "$SWAP_EXISTS" -eq 0 ]; then
    SWAP_MODE="partition"
    if [ -n "$NEXT_START" ]; then
        SWAP_MODE="file"
        SWAP_WHY="the tail of the disk is already taken by:${NEIGHBOURS}"
    elif [ "$ROOT_TARGET_SECTORS" -le 0 ]; then
        SWAP_MODE="file"
        SWAP_WHY="$(human "$SWAP_SECTORS") of swap would leave no room for the rootfs partition"
    elif [ "$PART_END" -gt "$ROOT_TARGET_END" ]; then
        # The rootfs partition currently reaches into where swap must go, so
        # this is the shrink case - only safe if the filesystem fits, and only
        # decidable at all if we know the filesystem's real size.
        if [ "$FS_EXACT" -eq 0 ]; then
            SWAP_MODE="file"
            SWAP_WHY="the rootfs partition must shrink to make room, and the exact filesystem size is unknown (dumpe2fs needs root)"
        elif [ $(( FS_SECTORS + SHRINK_MARGIN )) -gt "$ROOT_TARGET_SECTORS" ]; then
            SWAP_MODE="file"
            SWAP_WHY="the rootfs filesystem ($(human "$FS_SECTORS")) no longer fits in what would be left ($(human "$ROOT_TARGET_SECTORS")), and ext4 cannot shrink online"
        fi
    fi
fi

# Once swap is a partition, the rootfs stops in front of it. Once it is a
# file, the rootfs takes everything.
if [ "$SWAP_MODE" = "partition" ]; then
    ROOT_FINAL_END="$ROOT_TARGET_END"
else
    ROOT_FINAL_END="$ROOT_CEILING"
fi
ROOT_FINAL_SECTORS=$(( ROOT_FINAL_END - PART_START + 1 ))
FS_GAP=$(( ROOT_FINAL_SECTORS - FS_SECTORS ))
[ "$FS_GAP" -ge 0 ] || FS_GAP=0

# --- Report -----------------------------------------------------------------
# -P (POSIX output): plain `df` wraps onto a second line when the device name
# is long, and then `awk NR==2` reads the wrapped device name instead of the
# sizes. -P guarantees one line per filesystem.
FS_USED="$(df -Ph / | awk 'NR==2 {print $3}')"
FS_SIZE="$(df -Ph / | awk 'NR==2 {print $2}')"
[ "$FS_EXACT" -eq 1 ] && APPROX="" || APPROX=" (approx)"

echo "${SELF}: disk        ${DISK}  $(human "$DISK_SECTORS")"
echo "${SELF}: partition   ${ROOT_SRC} (no. ${PART_NUM})  $(human "$PART_SECTORS")"
echo "${SELF}: filesystem  ${ROOT_FS}, $(human "$FS_SECTORS")${APPROX} - ${FS_USED} used of ${FS_SIZE}"
if [ "$SWAP_EXISTS" -eq 1 ]; then
    if [ "$SWAP_ACTIVE_SECTORS" -gt 0 ]; then
        echo "${SELF}: swap        $(human "$SWAP_ACTIVE_SECTORS") active"
    else
        echo "${SELF}: swap        present but not enabled (${SWAP_PART:-$SWAPFILE})"
    fi
else
    echo "${SELF}: swap        none"
fi
echo "${SELF}: unallocated after the partition:      $(human "$FREE_SECTORS")"
echo "${SELF}: filesystem short of its partition by: $(human "$FS_GAP")"

# A few free sectors are just GPT/alignment padding, not space worth
# reclaiming. The same slack applies to both gaps.
MIN_WORTH_IT=$(( 64 * 1024 * 1024 / SECTOR ))

NEED_SWAP=0
[ "$SWAP_MODE" != "none" ] && NEED_SWAP=1

NEED_PART=0
NEED_FS=0
# The partition has to move if it does not already end where it should - in
# either direction. The shrink half of that only ever happens as part of
# making room for swap, and only after the fits-check above.
if [ "$PART_END" -ne "$ROOT_FINAL_END" ]; then
    if [ "$SWAP_MODE" = "partition" ]; then
        NEED_PART=1
    elif [ "$FREE_SECTORS" -ge "$MIN_WORTH_IT" ]; then
        NEED_PART=1
    fi
fi
[ "$FS_GAP" -ge "$MIN_WORTH_IT" ] && NEED_FS=1

if [ "$SWAP_MODE" = "file" ] && [ -n "$SWAP_WHY" ]; then
    say "swap will be a file at ${SWAPFILE}, not a partition - ${SWAP_WHY}"
fi
if [ "$SWAP_EXISTS" -eq 1 ] && [ "$SWAP_BYTES" -gt 0 ]; then
    say "swap already provisioned - leaving it alone"
fi
if [ "$NEED_PART" -eq 0 ] && [ -n "$NEIGHBOURS" ] && [ "$FREE_SECTORS" -lt "$MIN_WORTH_IT" ]; then
    say "the rootfs is bounded by:${NEIGHBOURS} - it grows only up to those"
fi

if [ "$NEED_PART" -eq 0 ] && [ "$NEED_FS" -eq 0 ] && [ "$NEED_SWAP" -eq 0 ]; then
    if [ "$SWAP_BYTES" -eq 0 ]; then
        say "nothing to do - the filesystem already fills the disk (swap not requested)"
    else
        say "nothing to do - the filesystem already fills the disk and swap is provisioned"
    fi
    exit 0
fi

[ "$SWAP_MODE" = "partition" ] && \
    say "would create a swap partition of $(human "$(( SWAP_END - SWAP_START + 1 ))") at the end of ${DISK}"
[ "$SWAP_MODE" = "file" ] && \
    say "would create a swapfile of $(human "$SWAP_SECTORS") at ${SWAPFILE}"
if [ "$NEED_PART" -eq 1 ]; then
    if [ "$PART_END" -gt "$ROOT_FINAL_END" ]; then
        say "would SHRINK partition ${PART_NUM} to $(human "$ROOT_FINAL_SECTORS") to make room (the filesystem, $(human "$FS_SECTORS"), stays where it is)"
    else
        say "would extend partition ${PART_NUM} to $(human "$ROOT_FINAL_SECTORS")"
    fi
fi
[ "$NEED_FS" -eq 1 ] && \
    say "would resize the ${ROOT_FS} filesystem to fill its partition"

[ "$MODE" = "status" ] && exit 0

# --- Everything past here writes, or describes what would be written --------
# Root is checked only for --grow: --dry-run just prints, and refusing to
# print without root would be friction for nothing.
[ "$MODE" = "grow" ] && { [ "$(id -u)" -eq 0 ] \
    || die "must run as root (try: sudo ${SELF} --grow)"; }
need resize2fs e2fsprogs-resize2fs
if [ "$NEED_PART" -eq 1 ] || [ "$SWAP_MODE" = "partition" ]; then
    need sgdisk gptfdisk
    need sfdisk util-linux-sfdisk
    need partx util-linux-partx
fi
if [ "$SWAP_MODE" != "none" ]; then
    need mkswap util-linux
    need swapon util-linux
fi

# The device node for a partition number is not something to guess at
# ("nvme0n1" + "1" is wrong, "nvme0n1p1" is right, and "sda" + "1" is right
# while "sdap1" is wrong). Ask the kernel instead: after partx -a, exactly
# one of the disk's children reports this partition number.
part_node_for() {
    for pfile in /sys/block/"${DISK_KNAME}"/*/partition; do
        [ -e "$pfile" ] || continue
        if [ "$(cat "$pfile")" = "$1" ]; then
            echo "/dev/$(basename "$(dirname "$pfile")")"
            return 0
        fi
    done
    return 1
}

# First free GPT entry, from the table rather than from the kernel - they can
# disagree mid-run, and the table is what sgdisk writes into.
next_part_num() {
    sgdisk -p "$DISK" 2>/dev/null \
        | awk '$1 ~ /^[0-9]+$/ {n = ($1 > n ? $1 : n)} END {print n + 1}'
}

if [ "$MODE" = "dryrun" ]; then
    # sgdisk cannot read the table without root, and next_part_num would then
    # report 1 - a real partition number, and the wrong one. --dry-run is
    # meant to be usable unprivileged, so say what is unknown instead.
    if [ "$SWAP_MODE" = "partition" ] && [ "$(id -u)" -eq 0 ]; then
        NEW_NUM="$(next_part_num)"
    else
        NEW_NUM="<next free>"
    fi
    say "would run:"
    if [ "$NEED_PART" -eq 1 ] || [ "$SWAP_MODE" = "partition" ]; then
        printf '    %s\n' "sgdisk --move-second-header ${DISK}"
    fi
    if [ "$NEED_PART" -eq 1 ]; then
        printf '    %s\n' \
            "echo ', ${ROOT_FINAL_SECTORS}' | sfdisk --no-reread --force -N ${PART_NUM} ${DISK}"
    fi
    if [ "$SWAP_MODE" = "partition" ]; then
        printf '    %s\n' \
            "sgdisk -n ${NEW_NUM}:${SWAP_START}:${SWAP_END} -t ${NEW_NUM}:8200 -c ${NEW_NUM}:${SWAP_PART_LABEL} ${DISK}"
    fi
    if [ "$NEED_PART" -eq 1 ]; then
        printf '    %s\n' "partx -u --nr ${PART_NUM} ${DISK}"
    fi
    if [ "$SWAP_MODE" = "partition" ]; then
        printf '    %s\n' \
            "partx -a --nr ${NEW_NUM} ${DISK}" \
            "mkswap -L ${SWAP_PART_LABEL} <the new partition>" \
            "swapon <the new partition>   (+ a UUID= line in /etc/fstab)"
    fi
    printf '    %s\n' "resize2fs ${ROOT_SRC}"
    if [ "$SWAP_MODE" = "file" ]; then
        printf '    %s\n' \
            "dd if=/dev/zero of=${SWAPFILE} bs=1M count=${SWAP_MB}" \
            "chmod 0600 ${SWAPFILE}" \
            "mkswap ${SWAPFILE}" \
            "swapon ${SWAPFILE}   (+ a line in /etc/fstab)"
    fi
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    if [ "$NEED_PART" -eq 1 ] || [ "$SWAP_MODE" = "partition" ]; then
        printf '%s: rewrite the partition table on %s, provision swap and resize the filesystem? [y/N] ' "$SELF" "$DISK"
    elif [ "$SWAP_MODE" = "file" ]; then
        printf '%s: resize the filesystem on %s and create %s? [y/N] ' "$SELF" "$ROOT_SRC" "$SWAPFILE"
    else
        printf '%s: resize the filesystem on %s? [y/N] ' "$SELF" "$ROOT_SRC"
    fi
    read -r reply || reply=""
    case "$reply" in
        y|Y|yes|YES) ;;
        *) die "aborted - nothing was changed" ;;
    esac
fi

# Appends to /etc/fstab only if nothing already refers to the same area.
# Called for both the partition and the file path.
add_fstab_swap() {
    # $1 = the fstab first field (UUID=... or a path)
    #
    # Compare the FIRST FIELD for equality rather than grepping the file for
    # the string. A substring match says "already there" for /swapfile when
    # only /swapfile2 is listed, and for a UUID that is merely a prefix of
    # another - and it treats its argument as a regex, so the dots and dashes
    # in a path or a UUID are metacharacters that happen to match themselves.
    if [ -e /etc/fstab ] \
        && awk -v want="$1" '$1 == want { found = 1 } END { exit !found }' /etc/fstab
    then
        say "/etc/fstab already refers to $1 - not adding it twice"
        return 0
    fi
    # An fstab whose last line has no terminating newline would otherwise get
    # this entry glued onto the end of it, silently corrupting that mount and
    # losing this one. $(...) strips trailing newlines, so a file that ends
    # correctly yields an empty result here and nothing is added.
    if [ -s /etc/fstab ] && [ -n "$(tail -c1 /etc/fstab)" ]; then
        printf '\n' >> /etc/fstab
    fi
    printf '%s\tnone\tswap\tsw\t0\t0\n' "$1" >> /etc/fstab
    say "added $1 to /etc/fstab"
}

if [ "$NEED_PART" -eq 1 ] || [ "$SWAP_MODE" = "partition" ]; then
    # A copy of the partition table before touching it, so a mistake is
    # recoverable with `sgdisk --load-backup=<file> <disk>` from a rescue
    # boot. Only taken when the table is actually about to change - the
    # filesystem-only path rewrites no partition data at all.
    BACKUP_DIR=/var/lib/boat
    BACKUP="${BACKUP_DIR}/gpt-backup-${DISK_KNAME}.bin"
    mkdir -p "$BACKUP_DIR"
    if [ -e "$BACKUP" ]; then
        # Keep the ORIGINAL, as-flashed table: a second run would otherwise
        # overwrite it with the already-grown one, which is not what you
        # would want to restore to.
        say "keeping the existing table backup at ${BACKUP}"
    else
        sgdisk --backup="$BACKUP" "$DISK" >/dev/null
        say "partition table backed up to ${BACKUP}"
    fi

    say "relocating the GPT backup header to the end of the disk"
    sgdisk --move-second-header "$DISK" >/dev/null
fi

if [ "$NEED_PART" -eq 1 ]; then
    # --force is not bravado: sfdisk refuses to touch a disk that is in use,
    # and / is mounted from this very disk, which is the whole premise of an
    # online resize. sfdisk -N edits exactly one entry and leaves every field
    # it is not given alone - crucially the type GUID, unique GUID (PARTUUID)
    # and name, which the bootloader and any PARTUUID= reference depend on.
    # --no-reread: the kernel cannot re-read the table of a disk with a
    # mounted partition, and that failure is not a reason to abort.
    #
    # An explicit sector count, not the ", +" ("take all following free
    # space") this used to use. ", +" cannot shrink, and it cannot stop in
    # front of a partition that does not exist yet - both of which the swap
    # tail needs. The count is computed once, above, from the same figures
    # that were printed and confirmed.
    if [ "$PART_END" -gt "$ROOT_FINAL_END" ]; then
        say "shrinking partition ${PART_NUM} to $(human "$ROOT_FINAL_SECTORS") to make room for swap"
    else
        say "extending partition ${PART_NUM} to $(human "$ROOT_FINAL_SECTORS")"
    fi
    echo ", ${ROOT_FINAL_SECTORS}" | sfdisk --no-reread --force -N "$PART_NUM" "$DISK" >/dev/null
fi

NEW_PART=""
if [ "$SWAP_MODE" = "partition" ]; then
    NEW_NUM="$(next_part_num)"
    say "creating swap partition ${NEW_NUM} at sectors ${SWAP_START}-${SWAP_END}"
    # sgdisk exits non-zero purely because BLKRRPART fails on a disk with a
    # mounted partition - the table itself is written. partx below is what
    # actually tells the kernel, so judge success on the partition appearing,
    # not on this exit status.
    sgdisk -n "${NEW_NUM}:${SWAP_START}:${SWAP_END}" \
           -t "${NEW_NUM}:8200" \
           -c "${NEW_NUM}:${SWAP_PART_LABEL}" "$DISK" >/dev/null 2>&1 || true
fi

if [ "$NEED_PART" -eq 1 ]; then
    # partx -u updates just this partition's size in the kernel's view, which
    # does work while it is mounted (a full BLKRRPART does not).
    # Fatal, not a warning. resize2fs takes its target size from the KERNEL's
    # idea of the partition, so if this did not take, resize2fs finds the old
    # size, does nothing, and the script goes on to report "done" with an
    # unchanged filesystem - the one outcome worse than failing.
    #
    # It also has to happen BEFORE the swap partition is added to the kernel:
    # while the rootfs still claims the whole disk, adding a partition in the
    # tail overlaps it and the kernel rejects it.
    say "updating the kernel's view of ${ROOT_SRC}"
    if ! partx -u --nr "$PART_NUM" "$DISK"; then
        err_msg="partx could not update the kernel's view of ${ROOT_SRC}."
        say "$err_msg"
        say "The partition table on disk IS changed; only the running kernel"
        say "has not noticed. Reboot and re-run '${SELF} --grow' to finish."
        exit 1
    fi
fi

if [ "$SWAP_MODE" = "partition" ]; then
    say "telling the kernel about partition ${NEW_NUM}"
    partx -a --nr "$NEW_NUM" "$DISK" \
        || die "partx could not add partition ${NEW_NUM} - the table on disk is written; reboot and re-run '${SELF} --grow'"
    NEW_PART="$(part_node_for "$NEW_NUM")" \
        || die "partition ${NEW_NUM} was created but no device node appeared for it - reboot and re-run '${SELF} --grow'"
    say "formatting ${NEW_PART} as swap"
    mkswap -L "$SWAP_PART_LABEL" "$NEW_PART" >/dev/null
    SWAP_UUID="$(blkid -s UUID -o value "$NEW_PART" 2>/dev/null || true)"
    if [ -n "$SWAP_UUID" ]; then
        add_fstab_swap "UUID=${SWAP_UUID}"
    else
        # No UUID to hang it on - fall back to the partition label, which
        # mkswap -L just set and which survives a re-enumeration the way a
        # bare /dev/nvme0n1p16 would not.
        add_fstab_swap "LABEL=${SWAP_PART_LABEL}"
    fi
    swapon "$NEW_PART" || die "could not enable swap on ${NEW_PART}"
    say "swap is on: $(human "$(( SWAP_END - SWAP_START + 1 ))") at ${NEW_PART}"
fi

# Always after any partition change, and on its own the whole job after a
# normal flash. resize2fs is a no-op when the filesystem already fills its
# partition, so running it unconditionally costs nothing.
say "resizing the ${ROOT_FS} filesystem on ${ROOT_SRC}"
resize2fs "$ROOT_SRC"

if [ "$SWAP_MODE" = "file" ]; then
    # After resize2fs, so the space this needs is space the filesystem
    # actually has.
    AVAIL_MB="$(df -Pm / | awk 'NR==2 {print $4}')"
    [ "$AVAIL_MB" -gt $(( SWAP_MB + 1024 )) ] \
        || die "only ${AVAIL_MB} MiB free on / - not creating a ${SWAP_MB} MiB swapfile"
    say "creating the swapfile at ${SWAPFILE}, ${SWAP_MB} MiB - this writes every byte, give it a minute"
    # dd, not fallocate. A fallocate'd file on ext4 is made of unwritten
    # extents, and swapon refuses those ("skipping - it appears to have
    # holes") because the swap path cannot allocate blocks at write time.
    # Writing zeros is slower and completely unambiguous.
    rm -f "$SWAPFILE"
    dd if=/dev/zero of="$SWAPFILE" bs=1M count="$SWAP_MB" status=none
    # Before mkswap, not after: for the moments in between, the file must not
    # be readable by anyone else - it is about to hold whatever the kernel
    # pages out.
    chmod 0600 "$SWAPFILE"
    mkswap "$SWAPFILE" >/dev/null
    add_fstab_swap "$SWAPFILE"
    swapon "$SWAPFILE" || die "could not enable swap on ${SWAPFILE}"
    say "swap is on: ${SWAP_MB} MiB at ${SWAPFILE}"
fi

say "done - / is now $(df -Ph / | awk 'NR==2 {print $2}'), swap $(free -h 2>/dev/null | awk '/^Swap:/ {print $2}' || echo '?')"
