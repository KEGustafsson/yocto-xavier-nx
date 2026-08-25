#!/bin/sh
# Grow the boat computer's root filesystem to fill the whole NVMe SSD.
#
#   boat-grow-rootfs                 # report what would change, touch nothing
#   boat-grow-rootfs --grow          # do it (asks for confirmation first)
#   boat-grow-rootfs --grow --yes    # do it without the prompt
#   boat-grow-rootfs --dry-run       # print the exact commands, run none
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
# reclaim has to happen once, on the device.
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
# WHY IT IS SAFE TO RUN ON THE RUNNING SYSTEM
# ext4 supports online resize, so / stays mounted throughout and no file data
# is rewritten. If the partition does need extending,
# APP (the rootfs) is the LAST partition in the layout meta-tegra flashes to
# the NVMe - kernel, kernel-dtb, the A/B chain reserves, recovery,
# RECROOTFS, esp, esp_alt, UDA, then APP (see
# yocto/flash/external-flash.xml.in). Growing it therefore only ever claims
# space that is already unallocated: no partition is moved and no file data
# is rewritten. ext4 supports online resize, so / stays mounted throughout.
# The script re-checks that "APP is last" itself rather than trusting the
# layout, and refuses if anything is allocated past the rootfs.
#
# If power is lost between the partition edit and resize2fs, just run it
# again - it picks up wherever it stopped.
set -eu

SELF=boat-grow-rootfs

MODE=status          # status | dryrun | grow
ASSUME_YES=0

say() { echo "${SELF}: $*"; }
die() { echo "${SELF}: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: boat-grow-rootfs [options]

Grow the root filesystem, and the partition holding it, to fill the whole
NVMe SSD. The image ships a deliberately small 16 GiB rootfs (it is flashed
byte-for-byte over USB 2.0); the rest of the drive is unallocated until this
is run once.

  -s, --status     report current and reclaimable space, change nothing
                   (the default)
  -n, --dry-run    run every check and print the exact commands, run none
  -g, --grow       actually grow the partition and the filesystem
  -y, --yes        skip the confirmation prompt (implies nothing on its own)
  -h, --help       this text

Needs root. Safe while / is mounted: the rootfs partition is the last one on
the disk, so only unallocated space is claimed and ext4 is resized online.
USAGE
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -s|--status)  MODE=status ;;
        -n|--dry-run) MODE=dryrun ;;
        -g|--grow)    MODE=grow ;;
        -y|--yes)     ASSUME_YES=1 ;;
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

# lsblk first: it answers from udev and so works unprivileged, which keeps
# --status usable without sudo. blkid -p probes the device itself and needs
# root, so it is only the fallback.
PT_TYPE="$(lsblk -dno PTTYPE "$DISK" 2>/dev/null | head -n1 || true)"
[ -n "$PT_TYPE" ] || PT_TYPE="$(blkid -p -o value -s PTTYPE "$DISK" 2>/dev/null || true)"
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
FREE_SECTORS=$(( LAST_USABLE - PART_END ))
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
# reserved blocks). That skew is harmless here - at worst it offers a
# resize2fs that turns out to be a no-op, never the reverse.
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
    FS_1K="$(df -k / 2>/dev/null | awk 'NR==2 {print $2}')"
    [ -n "$FS_1K" ] || FS_1K=0
    FS_SECTORS=$(( FS_1K * 2 ))
fi
FS_GAP=$(( PART_SECTORS - FS_SECTORS ))
[ "$FS_GAP" -ge 0 ] || FS_GAP=0

# --- Is anything allocated past the rootfs? --------------------------------
# Only blocks the partition-growing half; a filesystem resize inside the
# partition it already has is unaffected.
BLOCKERS=""
for pfile in /sys/class/block/"${DISK_KNAME}"*/partition; do
    [ -e "$pfile" ] || continue
    pdir="$(dirname "$pfile")"
    pname="$(basename "$pdir")"
    [ "$pname" = "$PART_KNAME" ] && continue
    pstart="$(cat "${pdir}/start")"
    if [ "$pstart" -gt "$PART_END" ]; then
        BLOCKERS="${BLOCKERS} ${pname}(starts at sector ${pstart})"
    fi
done

# --- Report -----------------------------------------------------------------
FS_USED="$(df -h / | awk 'NR==2 {print $3}')"
FS_SIZE="$(df -h / | awk 'NR==2 {print $2}')"
[ "$FS_EXACT" -eq 1 ] && APPROX="" || APPROX=" (approx)"

echo "${SELF}: disk        ${DISK}  $(human "$DISK_SECTORS")"
echo "${SELF}: partition   ${ROOT_SRC} (no. ${PART_NUM})  $(human "$PART_SECTORS")"
echo "${SELF}: filesystem  ${ROOT_FS}, $(human "$FS_SECTORS")${APPROX} - ${FS_USED} used of ${FS_SIZE}"
echo "${SELF}: unallocated after the partition:      $(human "$FREE_SECTORS")"
echo "${SELF}: filesystem short of its partition by: $(human "$FS_GAP")"

# A few free sectors are just GPT/alignment padding, not space worth
# reclaiming. The same slack applies to both gaps.
MIN_WORTH_IT=$(( 64 * 1024 * 1024 / SECTOR ))   # 64 MiB

NEED_PART=0
NEED_FS=0
[ "$FREE_SECTORS" -ge "$MIN_WORTH_IT" ] && NEED_PART=1
[ "$FS_GAP" -ge "$MIN_WORTH_IT" ] && NEED_FS=1

if [ "$NEED_PART" -eq 1 ] && [ -n "$BLOCKERS" ]; then
    say "cannot extend the partition - these are allocated after it:${BLOCKERS}"
    say "growing into them would destroy them. The filesystem can still be"
    say "resized inside the partition it already has, if that gap is non-zero."
    NEED_PART=0
fi

if [ "$NEED_PART" -eq 0 ] && [ "$NEED_FS" -eq 0 ]; then
    say "nothing to do - the filesystem already fills the disk"
    exit 0
fi

[ "$NEED_PART" -eq 1 ] && \
    say "would extend partition ${PART_NUM} to $(human "$(( LAST_USABLE - PART_START + 1 ))")"
[ "$NEED_FS" -eq 1 ] && \
    say "would resize the ${ROOT_FS} filesystem to fill its partition"

[ "$MODE" = "status" ] && exit 0

# --- Everything past here writes, or describes what would be written --------
# Root is checked only for --grow: --dry-run just prints, and refusing to
# print without root would be friction for nothing.
[ "$MODE" = "grow" ] && { [ "$(id -u)" -eq 0 ] \
    || die "must run as root (try: sudo ${SELF} --grow)"; }
need resize2fs e2fsprogs-resize2fs
if [ "$NEED_PART" -eq 1 ]; then
    need sgdisk gptfdisk
    need sfdisk util-linux-sfdisk
    need partx util-linux-partx
fi

if [ "$MODE" = "dryrun" ]; then
    say "would run:"
    if [ "$NEED_PART" -eq 1 ]; then
        printf '    %s\n' \
            "sgdisk --move-second-header ${DISK}" \
            "echo ', +' | sfdisk --no-reread --force -N ${PART_NUM} ${DISK}" \
            "partx -u --nr ${PART_NUM} ${DISK}"
    fi
    printf '    %s\n' "resize2fs ${ROOT_SRC}"
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    if [ "$NEED_PART" -eq 1 ]; then
        printf '%s: rewrite the partition table on %s and resize the filesystem? [y/N] ' "$SELF" "$DISK"
    else
        printf '%s: resize the filesystem on %s? [y/N] ' "$SELF" "$ROOT_SRC"
    fi
    read -r reply || reply=""
    case "$reply" in
        y|Y|yes|YES) ;;
        *) die "aborted - nothing was changed" ;;
    esac
fi

if [ "$NEED_PART" -eq 1 ]; then
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

    # sfdisk -N edits exactly one entry and leaves every field it is not
    # given alone - crucially the type GUID, unique GUID (PARTUUID) and name,
    # which the bootloader and any PARTUUID= reference depend on. ", +" means
    # "same start, extend over all following free space". --no-reread: the
    # kernel cannot re-read the table of a disk with a mounted partition, and
    # that failure is not a reason to abort.
    say "extending partition ${PART_NUM} to the end of the disk"
    echo ', +' | sfdisk --no-reread --force -N "$PART_NUM" "$DISK" >/dev/null

    # partx -u updates just this partition's size in the kernel's view, which
    # does work while it is mounted (a full BLKRRPART does not).
    say "updating the kernel's view of ${ROOT_SRC}"
    partx -u --nr "$PART_NUM" "$DISK" \
        || say "WARNING: partx failed; a reboot will pick up the new size"
fi

# Always last, and on its own the whole job after a normal flash. resize2fs
# is a no-op when the filesystem already fills its partition, so running it
# unconditionally costs nothing.
say "resizing the ${ROOT_FS} filesystem on ${ROOT_SRC}"
resize2fs "$ROOT_SRC"

say "done - / is now $(df -h / | awk 'NR==2 {print $2}')"
