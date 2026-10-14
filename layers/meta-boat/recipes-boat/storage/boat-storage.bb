SUMMARY = "A/B layout storage: SWAP, the shared DATA partition on /data, and filesystem growth"
DESCRIPTION = "On-board storage half of the A/B layout (BOAT_AB_LAYOUT). \
boat-storage enables the SWAP partition, checks the DATA partition before \
mounting it on /data, keeps the listed users' homes and a size-capped \
persistent journal there, and grows DATA and the flashed root filesystem \
into their partitions. Every shared mount is nofail: a missing or broken \
DATA never stops a slot from booting. It never formats DATA by itself. See \
docs/plans/ab-os-updates.md sections 5.2 and 5.8."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://boat-storage.sh \
    file://boat-swap.service \
    file://boat-data-check.service \
    file://data.mount.in \
    file://boat-data-prepare.service \
    file://home-user.mount.in \
    file://journal.mount.in \
    file://10-boat-ab-journal-flush.conf.in \
    file://boat-journal-flush.service.in \
    file://10-boat-ab-journald.conf \
    file://boat-storage-grow.service \
"

S = "${WORKDIR}"

inherit allarch systemd

# Users whose home is kept on DATA (/home/<user> bind-mounted from
# /data/home/<user>), so it is the same in both slots and survives updates.
# root is deliberately not one of them: its home (/home/root here) stays in
# the slot, so its SSH keys still work when DATA is what failed.
BOAT_AB_HOME_USERS ?= "boat"

# Where journald's persistent journal really is. With VOLATILE_LOG_DIR,
# /var/log is a symlink into the /var/volatile tmpfs, and a mount unit must
# name the real path.
BOAT_AB_JOURNAL_DIR = "${@'/var/volatile/log/journal' if bb.utils.to_boolean(d.getVar('VOLATILE_LOG_DIR')) else '/var/log/journal'}"

# The unit name systemd gives a mount (or swap) of PATH. Only paths that need
# no escaping are accepted, so the name can be built here without
# systemd-escape; anything else fails the parse.
def boat_mount_unit(d, path, suffix='.mount'):
    import re
    if not re.match(r'^(/[A-Za-z0-9_]+)+$', path):
        bb.fatal("boat-storage: cannot name a unit for %r - use a path of letters, digits and _ only" % path)
    return path.strip('/').replace('/', '-') + suffix

BOAT_AB_JOURNAL_UNIT = "${@boat_mount_unit(d, d.getVar('BOAT_AB_JOURNAL_DIR'))}"
# systemd's gpt-auto-generator writes this unit for a partition of the Linux
# swap type on the root disk - which SWAP is - and would enable it with none
# of boat-storage's checks (GPT name, a hibernation image left alone), racing
# boat-swap on the first boot. A unit file linked to /dev/null here masks it:
# the unit directory outranks the generator's output.
BOAT_AB_SWAP_UNIT = "${@boat_mount_unit(d, '%sp%s' % (d.getVar('BOAT_AB_DISK'), d.getVar('BOAT_AB_PARTNUM_SWAP')), '.swap')}"
BOAT_AB_HOME_UNITS = "${@' '.join(boat_mount_unit(d, '/home/' + u) for u in d.getVar('BOAT_AB_HOME_USERS').split())}"

do_install() {
    install -d ${D}${sbindir}
    sed -e 's|@HOME_USERS@|${BOAT_AB_HOME_USERS}|g' \
        -e 's|^\. /usr/lib/boat-ab/lib.sh$|. ${libdir}/boat-ab/lib.sh|' \
        ${WORKDIR}/boat-storage.sh > ${D}${sbindir}/boat-storage
    chmod 0755 ${D}${sbindir}/boat-storage

    install -d ${D}${systemd_system_unitdir}
    for u in boat-swap.service boat-data-check.service boat-data-prepare.service \
             boat-storage-grow.service; do
        install -m 0644 ${WORKDIR}/$u ${D}${systemd_system_unitdir}/
    done
    sed -e 's|@DATA_DEV@|${BOAT_AB_DISK}p${BOAT_AB_PARTNUM_DATA}|g' \
        ${WORKDIR}/data.mount.in > ${D}${systemd_system_unitdir}/data.mount
    sed -e 's|@JOURNAL_DIR@|${BOAT_AB_JOURNAL_DIR}|g' \
        ${WORKDIR}/journal.mount.in > ${D}${systemd_system_unitdir}/${BOAT_AB_JOURNAL_UNIT}
    for user in ${BOAT_AB_HOME_USERS}; do
        sed -e "s|@USER@|$user|g" ${WORKDIR}/home-user.mount.in \
            > ${D}${systemd_system_unitdir}/home-$user.mount
    done

    # The prepare step has to finish before every bind mount that needs the
    # directories it makes.
    install -d ${D}${systemd_system_unitdir}/boat-data-prepare.service.d
    printf '[Unit]\nBefore=%s\n' "${BOAT_AB_JOURNAL_UNIT} ${BOAT_AB_HOME_UNITS}" \
        > ${D}${systemd_system_unitdir}/boat-data-prepare.service.d/10-boat-ab-before.conf

    install -d ${D}${systemd_system_unitdir}/systemd-journal-flush.service.d
    sed -e 's|@JOURNAL_DIR@|${BOAT_AB_JOURNAL_DIR}|g' \
        ${WORKDIR}/10-boat-ab-journal-flush.conf.in \
        > ${D}${systemd_system_unitdir}/systemd-journal-flush.service.d/10-boat-ab.conf
    sed -e 's|@JOURNAL_UNIT@|${BOAT_AB_JOURNAL_UNIT}|g' \
        ${WORKDIR}/boat-journal-flush.service.in \
        > ${D}${systemd_system_unitdir}/boat-journal-flush.service
    chmod 0644 ${D}${systemd_system_unitdir}/boat-journal-flush.service
    install -d ${D}${systemd_unitdir}/journald.conf.d
    install -m 0644 ${WORKDIR}/10-boat-ab-journald.conf \
        ${D}${systemd_unitdir}/journald.conf.d/10-boat-ab.conf

    chmod 0644 ${D}${systemd_system_unitdir}/*.mount \
        ${D}${systemd_system_unitdir}/*.d/*.conf

    ln -sf /dev/null ${D}${systemd_system_unitdir}/${BOAT_AB_SWAP_UNIT}
}
do_install[vardeps] += "BOAT_AB_DISK BOAT_AB_PARTNUM_DATA BOAT_AB_PARTNUM_SWAP BOAT_AB_HOME_USERS BOAT_AB_JOURNAL_DIR"

python () {
    import re
    for user in d.getVar('BOAT_AB_HOME_USERS').split():
        if not re.match(r'^[a-z_][a-z0-9_]*$', user):
            bb.fatal("BOAT_AB_HOME_USERS: %r is not a user name this recipe can make a mount unit for" % user)
    # systemd-journal-flush.service has RequiresMountsFor=/var/log/journal,
    # which no drop-in can take away (an empty RequiresMountsFor= does not
    # reset it in systemd v250). With a persistent /var/log that path is the
    # journal's bind mount from DATA, and sysinit.target would wait for
    # DATA's fsck - however long a repair takes. With the volatile /var/log
    # it names no mount, and the drop-in skips the unit until DATA is there.
    if d.getVar('BOAT_AB_LAYOUT') == '1' and \
            not bb.utils.to_boolean(d.getVar('VOLATILE_LOG_DIR')):
        bb.fatal("boat-storage: the A/B layout keeps the journal on DATA and needs "
                 "VOLATILE_LOG_DIR = \"yes\" (poky's default): with a persistent "
                 "/var/log every boot would wait for DATA's filesystem check")
}

SYSTEMD_SERVICE:${PN} = "\
    boat-swap.service \
    data.mount \
    ${BOAT_AB_JOURNAL_UNIT} \
    boat-journal-flush.service \
    ${BOAT_AB_HOME_UNITS} \
    boat-storage-grow.service \
"

FILES:${PN} = "\
    ${sbindir}/boat-storage \
    ${systemd_system_unitdir} \
    ${systemd_unitdir}/journald.conf.d \
"

# boat-ab-boot     - /usr/lib/boat-ab/lib.sh: partition numbers, GPT name
#                    checks, the booted slot
# e2fsprogs-*      - e2fsck -p before mounting DATA, online resize2fs,
#                    mkfs.ext4 for format-data
# util-linux-*     - mkswap/swapon, "blkid -p" for the GPT name and the
#                    filesystem signature, mountpoint
RDEPENDS:${PN} = "\
    boat-ab-boot \
    e2fsprogs-e2fsck \
    e2fsprogs-resize2fs \
    e2fsprogs-mke2fs \
    util-linux-mkswap \
    util-linux-swapon \
    util-linux-blkid \
    util-linux-mountpoint \
"
