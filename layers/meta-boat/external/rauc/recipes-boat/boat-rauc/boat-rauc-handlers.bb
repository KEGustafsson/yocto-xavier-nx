SUMMARY = "RAUC install handlers for the boat computer's A/B layout"
DESCRIPTION = "RAUC's system pre-install handler refuses an install unless \
the board came up through GRUB from a committed slot, /data is mounted, \
the target partitions are the ones the layout puts at those numbers, and \
the bundle is for the running L4T release; it then marks the target slot \
unusable and not committed until RAUC activates it. The post-install \
handler keeps grubenv.bak in step and records the install. See \
docs/plans/ab-os-updates.md section 5.6."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://boat-rauc-pre-install.sh \
    file://boat-rauc-post-install.sh \
"

S = "${WORKDIR}"

inherit allarch

do_configure[noexec] = "1"
do_compile[noexec] = "1"

# The paths system.conf's [handlers] names.
do_install() {
    install -d ${D}${nonarch_libdir}/boat-rauc
    install -m 0755 ${WORKDIR}/boat-rauc-pre-install.sh ${D}${nonarch_libdir}/boat-rauc/pre-install
    install -m 0755 ${WORKDIR}/boat-rauc-post-install.sh ${D}${nonarch_libdir}/boat-rauc/post-install
}

FILES:${PN} = "${nonarch_libdir}/boat-rauc"

# boat-ab-boot: /usr/lib/boat-ab/lib.sh (layout checks, grubenv writes).
# nv-tegra-release: /etc/nv_tegra_release, the running L4T release.
RDEPENDS:${PN} = "boat-ab-boot nv-tegra-release"
