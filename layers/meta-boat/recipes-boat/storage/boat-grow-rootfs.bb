SUMMARY = "Grow the flashed rootfs to fill the whole NVMe SSD"
DESCRIPTION = "Ships boat-grow-rootfs, a root command run once on the device \
to claim the SSD space the fixed-size flashed image leaves unallocated. The \
image is built with a deliberately small 16 GiB rootfs (ROOTFS_SIZE_BYTES in \
scripts/env.sh) because make-sdcard writes every byte of it over recovery-mode \
USB 2.0, sparse or not; this moves the \
GPT backup header to the end of the drive, extends the APP partition into the \
free space and resizes the ext4 online. See \
docs/05-phase2-boat-computer-layer.md 'Reclaiming the rest of the SSD'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://boat-grow-rootfs.sh"

S = "${WORKDIR}"

inherit allarch

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-grow-rootfs.sh ${D}${bindir}/boat-grow-rootfs
}

FILES:${PN} = "${bindir}/boat-grow-rootfs"

# Deliberately NOT a systemd unit that fires on first boot. Rewriting a
# partition table is the one operation on this image that can lose the whole
# rootfs if the disk turns out not to match the expected layout, and it buys
# nothing to do it unattended before anyone has looked at the machine. The
# script's default mode is read-only for the same reason: `boat-grow-rootfs`
# reports, `boat-grow-rootfs --grow` acts.
#
# The tools it drives, all of which it checks for at runtime with a message
# naming the package:
#   gptfdisk            - sgdisk --move-second-header, and the table backup
#   util-linux-sfdisk   - the single-entry grow that preserves the PARTUUID
#   util-linux-partx    - re-read one partition's size while / is mounted
#   util-linux-findmnt  - resolve what / is actually mounted from
#   util-linux-lsblk    - map that partition back to its parent disk
#   util-linux-blkid    - confirm the table is GPT before touching it
#   e2fsprogs-resize2fs - the online ext4 resize itself
#   e2fsprogs-dumpe2fs  - read the filesystem's own block count, to see
#                         whether it is smaller than the partition holding it
#                         (the normal case after a flash - see the header of
#                         boat-grow-rootfs.sh)
# util-linux's own subpackages are already in any image via the util-linux
# RRECOMMENDS, but naming them here keeps this package honest on a smaller
# one. e2fsprogs-resize2fs is NOT installed by default (e2fsprogs pulls in
# e2fsck/mke2fs/dumpe2fs/badblocks but not resize2fs) - without it listed
# here the script would get all the way to the last step and fail.
RDEPENDS:${PN} = "\
    gptfdisk \
    util-linux-sfdisk \
    util-linux-partx \
    util-linux-findmnt \
    util-linux-lsblk \
    util-linux-blkid \
    e2fsprogs-resize2fs \
    e2fsprogs-dumpe2fs \
"
