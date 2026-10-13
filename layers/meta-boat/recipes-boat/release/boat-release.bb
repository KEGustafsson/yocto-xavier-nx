SUMMARY = "/etc/boat-release: the release this image was built as"
DESCRIPTION = "Writes BOAT_RELEASE (scripts/env.sh) to /etc/boat-release. An \
A/B update bundle carries the same string as its version, so a slot that \
was flashed and a slot that was installed from a bundle report their \
version the same way. See docs/plans/ab-os-updates.md section 5.10."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

inherit allarch

S = "${WORKDIR}"

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${sysconfdir}
    echo "${BOAT_RELEASE_EFFECTIVE}" > ${D}${sysconfdir}/boat-release
    chmod 0644 ${D}${sysconfdir}/boat-release
}
# A new release has to rebuild this, and with it the image: the bundle
# checks that the image it carries says the same version it does.
do_install[vardeps] += "BOAT_RELEASE_EFFECTIVE"

FILES:${PN} = "${sysconfdir}/boat-release"
