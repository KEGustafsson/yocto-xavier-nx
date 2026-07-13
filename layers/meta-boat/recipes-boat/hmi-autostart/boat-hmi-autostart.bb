SUMMARY = "Autologin + Weston autostart for the boat helm display"
DESCRIPTION = "Autologins BOAT_HMI_USER on tty1 and launches Weston from \
that session, so systemd-logind creates /run/user/<uid>/wayland-1 for a \
containerized HMI (e.g. Firefox) to mount. See \
docs/05-phase2-boat-computer-layer.md."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://autologin.conf file://boat-weston-autostart.sh"

S = "${WORKDIR}"

# Must match the login user created via extrausers in boat-image.bb.
BOAT_HMI_USER ?= "boat"
BOAT_HMI_UID ?= "2000"

do_install() {
    install -d ${D}${systemd_system_unitdir}/getty@tty1.service.d
    install -m 0644 ${WORKDIR}/autologin.conf \
        ${D}${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf
    sed -i -e 's,@BOAT_HMI_USER@,${BOAT_HMI_USER},g' \
        ${D}${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf

    install -d ${D}${sysconfdir}/profile.d
    install -m 0755 ${WORKDIR}/boat-weston-autostart.sh \
        ${D}${sysconfdir}/profile.d/boat-weston-autostart.sh
    sed -i -e 's,@BOAT_HMI_UID@,${BOAT_HMI_UID},g' \
        ${D}${sysconfdir}/profile.d/boat-weston-autostart.sh
}

FILES:${PN} = "\
    ${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf \
    ${sysconfdir}/profile.d/boat-weston-autostart.sh \
"

RDEPENDS:${PN} = "weston-init"
