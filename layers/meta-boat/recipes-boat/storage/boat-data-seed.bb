SUMMARY = "First filesystem for the A/B layout's DATA partition"
DESCRIPTION = "A small, empty ext4 image (label boat-data) that the flash \
writes into the DATA partition (meta-tegra's DATAFILE / \
IMAGE_TEGRAFLASH_DATA). The first boot grows it online to fill the \
partition. Building it here means the board never has to decide on its own \
to format a partition that might hold someone's data. See \
docs/plans/ab-os-updates.md section 5.2."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

inherit deploy nopackages

DEPENDS = "e2fsprogs-native"

S = "${WORKDIR}"

BOAT_DATA_SEED = "boat-data-seed.ext4"
# Small, because only these bytes cross the flasher's USB link.
BOAT_DATA_SEED_MB ?= "512"

do_configure[noexec] = "1"

# The options are the ones a filesystem made for a large disk would get,
# since that is what this one becomes:
#   -b 4096           mke2fs would pick 1 KiB blocks for an image this size,
#                     and the block size can never change afterwards
#   -i 16384          one inode per 16 KiB, the ratio for an ordinary disk
#   -J size=64        the journal does not grow with the filesystem
#   -E resize=...     reserve group descriptor blocks for online growth to
#                     8 TiB (2^31 4 KiB blocks), beyond any SSD this board takes
#   -E root_owner     root-owned, without running mke2fs as root
#   -m 1              keep 1%, not 5%, back for root on a data disk
do_compile() {
    rm -f ${B}/${BOAT_DATA_SEED}
    dd if=/dev/zero of=${B}/${BOAT_DATA_SEED} bs=1M count=0 seek=${BOAT_DATA_SEED_MB} status=none
    E2FSPROGS_FAKE_TIME="${SOURCE_DATE_EPOCH}" \
        mkfs.ext4 -F -q -b 4096 -i 16384 -J size=64 -m 1 -L boat-data \
            -E root_owner=0:0,resize=2147483648 ${B}/${BOAT_DATA_SEED}
}

do_deploy() {
    install -m 0644 ${B}/${BOAT_DATA_SEED} ${DEPLOYDIR}/${BOAT_DATA_SEED}
}
addtask deploy after do_compile before do_build
