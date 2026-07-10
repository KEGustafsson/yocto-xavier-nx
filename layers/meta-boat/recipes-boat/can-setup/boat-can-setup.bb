SUMMARY = "Bring up the Xavier NX CAN interface for NMEA 2000 at boot"
DESCRIPTION = "systemd unit that configures can0 (mttcan controller) at the \
NMEA 2000 bitrate of 250 kbit/s and brings the link up."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://boat-can0.service \
    file://boat-can0.conf \
    "

inherit systemd allarch

SYSTEMD_SERVICE:${PN} = "boat-can0.service"

do_install() {
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-can0.service ${D}${systemd_system_unitdir}/

    # Editable config: change BITRATE / IFACE here without rebuilding the image
    # if you keep an overlay, or override via a bbappend.
    install -d ${D}${sysconfdir}/default
    install -m 0644 ${WORKDIR}/boat-can0.conf ${D}${sysconfdir}/default/boat-can0
}

FILES:${PN} += "${systemd_system_unitdir} ${sysconfdir}/default/boat-can0"
