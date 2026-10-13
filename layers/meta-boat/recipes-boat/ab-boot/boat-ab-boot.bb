SUMMARY = "A/B boot plumbing: keep GRUB in charge, guard its state, confirm boots"
DESCRIPTION = "On-board half of the A/B boot path for an A/B boat image \
(BOAT_AB_LAYOUT). boat-bootmode-guard keeps the UEFI variable \
L4TDefaultBootMode at 0 so L4TLauncher hands off to GRUB, and records whether \
this boot came through GRUB. boat-grubenv-guard validates GRUB's boot state on \
the ESP and rebuilds it when a power cut tore it. boat-ab-commit confirms the \
booted slot (a Phase 1 stub with no health gate - Phase 4 replaces it). The \
ESP is mounted by device and partition number, not by label. See \
docs/plans/ab-os-updates.md sections 5.1-5.4."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://boat-ab-lib.sh \
    file://boat-bootmode-guard.sh \
    file://boat-bootmode-guard.service \
    file://boat-grubenv-guard.sh \
    file://boat-grubenv-guard.service \
    file://boat-ab-commit.sh \
    file://boat-ab-commit.service \
    file://10-boat-ab-esp.conf \
"

S = "${WORKDIR}"

inherit allarch systemd

# systemd's name for the ESP's device (its fsck instance), e.g.
# dev-nvme0n1p12. Built here rather than with systemd-escape, so only a
# BOAT_AB_DISK that needs no escaping is accepted.
BOAT_AB_ESP_UNIT = "${@d.getVar('BOAT_AB_DISK').strip('/').replace('/', '-')}p${BOAT_AB_PARTNUM_ESP}"

python () {
    import re
    if not re.match(r'^/dev/[a-z0-9]+$', d.getVar('BOAT_AB_DISK')):
        bb.fatal("BOAT_AB_DISK must look like /dev/nvme0n1, got %r" % d.getVar('BOAT_AB_DISK'))
    # Said here, at parse time, rather than as "Nothing RPROVIDES rauc"
    # half way into an image build.
    if d.getVar('BOAT_AB_LAYOUT') == '1' and \
            'rauc' not in (d.getVar('BBFILE_COLLECTIONS') or '').split():
        bb.fatal("BOAT_AB_LAYOUT = \"1\" needs meta-rauc in bblayers.conf "
                 "(scripts/02-configure-build.sh with AB_LAYOUT=1, or kas/ab-updates.yml, adds it)")
}

do_install() {
    install -d ${D}${libdir}/boat-ab
    sed -e 's|@DISK@|${BOAT_AB_DISK}|g' \
        -e 's|@PARTNUM_A@|${BOAT_AB_PARTNUM_APP_A}|g' \
        -e 's|@PARTNUM_B@|${BOAT_AB_PARTNUM_APP_B}|g' \
        -e 's|@PARTNUM_ESP@|${BOAT_AB_PARTNUM_ESP}|g' \
        -e 's|@PARTNUM_SWAP@|${BOAT_AB_PARTNUM_SWAP}|g' \
        -e 's|@PARTNUM_DATA@|${BOAT_AB_PARTNUM_DATA}|g' \
        ${WORKDIR}/boat-ab-lib.sh > ${D}${libdir}/boat-ab/lib.sh
    chmod 0644 ${D}${libdir}/boat-ab/lib.sh

    install -d ${D}${sbindir}
    install -m 0755 ${WORKDIR}/boat-bootmode-guard.sh ${D}${sbindir}/boat-bootmode-guard
    install -m 0755 ${WORKDIR}/boat-grubenv-guard.sh ${D}${sbindir}/boat-grubenv-guard
    install -m 0755 ${WORKDIR}/boat-ab-commit.sh ${D}${sbindir}/boat-ab-commit

    install -d ${D}${systemd_system_unitdir}
    for u in boat-bootmode-guard boat-grubenv-guard boat-ab-commit; do
        install -m 0644 ${WORKDIR}/$u.service ${D}${systemd_system_unitdir}/
    done

    # meta-tegra's ESP mount unit is boot-efi.mount (ESPMOUNT = /boot/efi).
    install -d ${D}${systemd_system_unitdir}/boot-efi.mount.d
    sed -e 's|@DISK@|${BOAT_AB_DISK}|g' \
        -e 's|@PARTNUM_ESP@|${BOAT_AB_PARTNUM_ESP}|g' \
        -e 's|@ESP_UNIT@|${BOAT_AB_ESP_UNIT}|g' \
        ${WORKDIR}/10-boat-ab-esp.conf \
        > ${D}${systemd_system_unitdir}/boot-efi.mount.d/10-boat-ab-esp.conf
    chmod 0644 ${D}${systemd_system_unitdir}/boot-efi.mount.d/10-boat-ab-esp.conf

    # The scripts source the library from its installed path.
    sed -i -e 's|^\. /usr/lib/boat-ab/lib.sh$|. ${libdir}/boat-ab/lib.sh|' \
        ${D}${sbindir}/boat-bootmode-guard ${D}${sbindir}/boat-grubenv-guard \
        ${D}${sbindir}/boat-ab-commit
}
do_install[vardeps] += "BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B BOAT_AB_PARTNUM_ESP \
    BOAT_AB_PARTNUM_SWAP BOAT_AB_PARTNUM_DATA BOAT_AB_ESP_UNIT"

SYSTEMD_SERVICE:${PN} = "boat-bootmode-guard.service boat-grubenv-guard.service boat-ab-commit.service"

FILES:${PN} = "\
    ${libdir}/boat-ab \
    ${sbindir}/boat-bootmode-guard \
    ${sbindir}/boat-grubenv-guard \
    ${sbindir}/boat-ab-commit \
    ${systemd_system_unitdir} \
"

# grub-editenv     - reads and writes GRUB's boot state on the ESP
# efivar           - writes L4TDefaultBootMode (it handles efivarfs's
#                    immutable flag; meta-tegra's setup-nv-boot-control uses it
#                    the same way)
# util-linux-blkid - "blkid -p -s PART_ENTRY_NAME": the GPT name check behind
#                    every action; busybox's blkid cannot probe it
# util-linux-mountpoint - mountpoint
# coreutils        - od, and sync FILE for the grubenv writes (cmp comes
#                    from busybox)
# util-linux-fsck, dosfstools - systemd-fsck's fsck and fsck.vfat, for the
#                    ESP check before it is mounted
# setup-nv-boot-control-service - boot-efi.mount, the unit the ESP drop-in
#                    above amends. Otherwise only tegra-redundant-boot
#                    (MACHINE_EXTRA_RDEPENDS) brings it, which an image
#                    without packagegroup-base never gets.
RDEPENDS:${PN} = "\
    grub-editenv \
    efivar \
    util-linux-blkid \
    util-linux-mountpoint \
    util-linux-fsck \
    dosfstools \
    coreutils \
    setup-nv-boot-control-service \
"
