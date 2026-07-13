SUMMARY = "docker-compose bring-up unit + example stacks for the boat computer"
DESCRIPTION = "Ships example compose files (Signal K, DeepStream, Firefox) as \
read-only reference under /usr/share/boat/compose-examples/, and a systemd \
unit that runs the operator's own docker-compose stack from /data/compose \
(git-managed config-as-code) if one has been seeded there. See \
docs/05-phase2-boat-computer-layer.md 'Docker host setup'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://boat-compose-up.sh \
    file://boat-compose.service \
    file://signalk.yml.example \
    file://deepstream.yml.example \
    file://firefox.yml.example \
    file://x11-app.yml.example \
    file://signalk-kiosk.yml.example \
    "

inherit systemd allarch

# The wrapper shells out to docker-compose (python3-docker-compose, the only
# compose client on this project's kirkstone-era meta-virtualization).
RDEPENDS:${PN} = "docker-ce python3-docker-compose"

SYSTEMD_SERVICE:${PN} = "boat-compose.service"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-compose-up.sh ${D}${bindir}/boat-compose-up

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-compose.service ${D}${systemd_system_unitdir}/

    install -d ${D}${datadir}/boat/compose-examples
    install -m 0644 ${WORKDIR}/signalk.yml.example ${WORKDIR}/deepstream.yml.example \
        ${WORKDIR}/firefox.yml.example ${WORKDIR}/x11-app.yml.example \
        ${WORKDIR}/signalk-kiosk.yml.example \
        ${D}${datadir}/boat/compose-examples/
}

FILES:${PN} += "\
    ${bindir}/boat-compose-up \
    ${systemd_system_unitdir}/boat-compose.service \
    ${datadir}/boat/compose-examples \
"
