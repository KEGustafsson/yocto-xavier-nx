SUMMARY = "docker-compose bring-up unit + example stacks for the boat computer"
DESCRIPTION = "Ships example compose files (Signal K, DeepStream, Firefox) as \
read-only reference under /usr/share/boat/compose-examples/, and a systemd \
unit that runs the operator's own docker-compose stack from /data/compose \
(git-managed config-as-code) if one has been seeded there. See \
docs/05-phase2-boat-computer-layer.md 'Docker host setup'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://compose-examples.README \
    file://boat-compose-up.sh \
    file://boat-compose.service \
    file://signalk.yml.example \
    file://deepstream.yml.example \
    file://firefox.yml.example \
    file://x11-app.yml.example \
    file://browser.yml.example \
    file://Dockerfile.firefox.example \
    "

S = "${WORKDIR}"

inherit systemd allarch features_check

# The unit below is only ever enabled by systemd.bbclass under a systemd
# distro; on a sysvinit build it would be installed, packaged, and silently
# never run. Fail at parse time instead.
REQUIRED_DISTRO_FEATURES = "virtualization systemd"

# boat-compose-up runs `docker compose` - the v2 CLI PLUGIN, which is
# boat-docker-compose-plugin in this layer. It deliberately does NOT run the
# hyphenated v1 client: python3-docker-compose (the only compose client this
# project's kirkstone-era meta-virtualization packages) fails on this image
# with "ModuleNotFoundError: No module named 'distutils'" - see the header of
# boat-compose-up.sh. Depending on the broken client and not on the working one
# happened to work only because packagegroup-boat-containers pulls the plugin
# in; on any image without that packagegroup, boat-compose.service failed every
# boot with "docker: 'compose' is not a docker command".
RDEPENDS:${PN} = "docker-ce boat-docker-compose-plugin"

SYSTEMD_SERVICE:${PN} = "boat-compose.service"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-compose-up.sh ${D}${bindir}/boat-compose-up

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-compose.service ${D}${systemd_system_unitdir}/

    install -d ${D}${datadir}/boat/compose-examples
    install -m 0644 ${WORKDIR}/compose-examples.README \
        ${D}${datadir}/boat/compose-examples/README
    install -m 0644 ${WORKDIR}/signalk.yml.example ${WORKDIR}/deepstream.yml.example \
        ${WORKDIR}/firefox.yml.example ${WORKDIR}/x11-app.yml.example \
        ${WORKDIR}/browser.yml.example \
        ${WORKDIR}/Dockerfile.firefox.example \
        ${D}${datadir}/boat/compose-examples/
}

FILES:${PN} += "\
    ${bindir}/boat-compose-up \
    ${systemd_system_unitdir}/boat-compose.service \
    ${datadir}/boat/compose-examples \
"
