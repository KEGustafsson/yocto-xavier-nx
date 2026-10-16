SUMMARY = "A/B boot plumbing: keep GRUB in charge, guard its state, gate commits"
DESCRIPTION = "On-board half of the A/B boot path for an A/B boat image \
(BOAT_AB_LAYOUT). boat-bootmode-guard keeps the UEFI variable \
L4TDefaultBootMode at 0 so L4TLauncher hands off to GRUB, and records whether \
this boot came through GRUB. boat-grubenv-guard validates GRUB's boot state on \
the ESP and rebuilds it when a power cut tore it. boat-commit is the health \
gate: it commits a slot on trial only once its checks pass, and otherwise \
marks it bad and reboots into the other slot; a trial deadline, a sleep \
inhibitor and an emergency-mode reboot make sure every failure ends in that \
reboot, and systemd owns the hardware watchdog. The ESP is mounted by device \
and partition number, not by label. See docs/plans/ab-os-updates.md sections \
5.1-5.5."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://boat-ab-lib.sh \
    file://grub-editenv-wrapper.sh \
    file://boat-bootmode-guard.sh \
    file://boat-bootmode-guard.service \
    file://boat-grubenv-guard.sh \
    file://boat-grubenv-guard.service \
    file://boat-commit.sh \
    file://boat-commit.service \
    file://boat-trial-deadline.service \
    file://boat-trial-deadline.timer \
    file://boat-trial-inhibit.service \
    file://boat-emergency-reboot.service \
    file://health.conf \
    file://10-boat-ab-esp.conf \
    file://10-boat-ab-watchdog.conf \
    file://10-boat-ab-sshd.conf \
    file://10-boat-ab-sleep.conf \
    file://boat-ab-tmpfiles.conf \
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
    # GRUB loads /boot/Image and /boot/initrd from the slot, and only
    # meta-tegra's l4t-launcher-extlinux puts them there - which it adds
    # only with UBOOT_EXTLINUX = "1". Without it the image builds and
    # flashes, and then neither slot ever boots.
    if d.getVar('BOAT_AB_LAYOUT') == '1' and d.getVar('UBOOT_EXTLINUX') != '1':
        bb.fatal("BOAT_AB_LAYOUT = \"1\" needs UBOOT_EXTLINUX = \"1\": it puts /boot/Image and "
                 "/boot/initrd into each slot for GRUB (scripts/02-configure-build.sh with "
                 "AB_LAYOUT=1, or kas/ab-updates.yml, sets it)")
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
    # First on rauc.service's PATH (meta-boat's rauc drop-in).
    install -d ${D}${libdir}/boat-ab/bin
    install -m 0755 ${WORKDIR}/grub-editenv-wrapper.sh ${D}${libdir}/boat-ab/bin/grub-editenv

    install -d ${D}${sbindir}
    install -m 0755 ${WORKDIR}/boat-bootmode-guard.sh ${D}${sbindir}/boat-bootmode-guard
    install -m 0755 ${WORKDIR}/boat-grubenv-guard.sh ${D}${sbindir}/boat-grubenv-guard
    install -m 0755 ${WORKDIR}/boat-commit.sh ${D}${sbindir}/boat-commit

    install -d ${D}${systemd_system_unitdir}
    for u in boat-bootmode-guard.service boat-grubenv-guard.service \
             boat-commit.service boat-trial-deadline.service \
             boat-trial-deadline.timer boat-trial-inhibit.service \
             boat-emergency-reboot.service; do
        install -m 0644 ${WORKDIR}/$u ${D}${systemd_system_unitdir}/
    done

    # No suspend during a trial or an install, whoever asks (boat-commit
    # --may-sleep); no connection flood can fail sshd.socket, which the
    # health gate requires.
    for u in systemd-suspend systemd-hibernate systemd-hybrid-sleep \
             systemd-suspend-then-hibernate; do
        install -d ${D}${systemd_system_unitdir}/$u.service.d
        install -m 0644 ${WORKDIR}/10-boat-ab-sleep.conf \
            ${D}${systemd_system_unitdir}/$u.service.d/10-boat-ab-sleep.conf
    done
    install -d ${D}${systemd_system_unitdir}/sshd.socket.d
    install -m 0644 ${WORKDIR}/10-boat-ab-sshd.conf \
        ${D}${systemd_system_unitdir}/sshd.socket.d/10-boat-ab-sshd.conf

    # The A/B lock (and boat-update's) root-only from the start of the boot.
    install -d ${D}${nonarch_libdir}/tmpfiles.d
    install -m 0644 ${WORKDIR}/boat-ab-tmpfiles.conf ${D}${nonarch_libdir}/tmpfiles.d/boat-ab.conf

    install -d ${D}${sysconfdir}/boat-update
    install -m 0644 ${WORKDIR}/health.conf ${D}${sysconfdir}/boat-update/health.conf

    # systemd, not a daemon, owns /dev/watchdog on this layout (plan D8).
    install -d ${D}${systemd_unitdir}/system.conf.d
    install -m 0644 ${WORKDIR}/10-boat-ab-watchdog.conf \
        ${D}${systemd_unitdir}/system.conf.d/10-boat-ab-watchdog.conf

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
        ${D}${sbindir}/boat-commit ${D}${libdir}/boat-ab/bin/grub-editenv
    sed -i -e 's|/usr/bin/grub-editenv|${bindir}/grub-editenv|' \
        ${D}${libdir}/boat-ab/bin/grub-editenv
}
do_install[vardeps] += "BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B BOAT_AB_PARTNUM_ESP \
    BOAT_AB_PARTNUM_SWAP BOAT_AB_PARTNUM_DATA BOAT_AB_ESP_UNIT"

SYSTEMD_SERVICE:${PN} = "\
    boat-bootmode-guard.service \
    boat-grubenv-guard.service \
    boat-commit.service \
    boat-trial-deadline.timer \
    boat-trial-inhibit.service \
    boat-emergency-reboot.service \
"

FILES:${PN} = "\
    ${libdir}/boat-ab \
    ${sbindir}/boat-bootmode-guard \
    ${sbindir}/boat-grubenv-guard \
    ${sbindir}/boat-commit \
    ${systemd_system_unitdir} \
    ${systemd_unitdir}/system.conf.d \
    ${sysconfdir}/boat-update/health.conf \
    ${nonarch_libdir}/tmpfiles.d/boat-ab.conf \
"

# The health gate's settings are the operator's to tune.
CONFFILES:${PN} = "${sysconfdir}/boat-update/health.conf"

# grub-editenv     - reads and writes GRUB's boot state on the ESP
# efivar           - writes L4TDefaultBootMode (it handles efivarfs's
#                    immutable flag; meta-tegra's setup-nv-boot-control uses it
#                    the same way)
# util-linux-blkid - "blkid -p -s PART_ENTRY_NAME": the GPT name check behind
#                    every action; busybox's blkid cannot probe it
# util-linux-mountpoint - mountpoint
# coreutils        - od, sync FILE for the grubenv writes, timeout for the
#                    operator's health checks, "sleep infinity" for the trial
#                    inhibitor, readlink -f for the grub-editenv wrapper (cmp
#                    comes from busybox)
# util-linux-fsck, dosfstools - systemd-fsck's fsck and fsck.vfat, for the
#                    ESP check before it is mounted
# tegra-redundant-boot - nv_update_verifier, which resets NVIDIA's rootfs
#                    retry count on every boot that reaches Linux: the 2+1
#                    attempt budget (plan 5.3) only holds with it. Otherwise
#                    only MACHINE_EXTRA_RDEPENDS brings it, which an image
#                    without packagegroup-base never gets.
# setup-nv-boot-control-service - boot-efi.mount, the unit the ESP drop-in
#                    above amends (tegra-redundant-boot needs it too)
# util-linux-flock - the A/B lock: one grubenv writer at a time
RDEPENDS:${PN} = "\
    grub-editenv \
    efivar \
    util-linux-blkid \
    util-linux-mountpoint \
    util-linux-fsck \
    dosfstools \
    coreutils \
    tegra-redundant-boot \
    setup-nv-boot-control-service \
    util-linux-flock \
"
