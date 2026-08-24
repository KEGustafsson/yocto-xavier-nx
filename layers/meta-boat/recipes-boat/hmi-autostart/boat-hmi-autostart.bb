SUMMARY = "Autologin + XFCE desktop autostart for the boat helm display"
DESCRIPTION = "Autologins BOAT_HMI_USER on tty1 and starts Xorg + the XFCE \
desktop from that session, so the desktop runs unprivileged on a real \
systemd-logind seat and containerized GUI apps can reach the display over \
/tmp/.X11-unix. See docs/05-phase2-boat-computer-layer.md."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://autologin.conf \
           file://boat-xfce-autostart.sh \
           file://boat-xfce-session \
           file://10-boat-keyboard.conf \
"

S = "${WORKDIR}"

# Fail loudly at parse time if the distro was configured without x11
# (see scripts/02-configure-build.sh) rather than shipping a desktop
# autostart into an image that has no X server to start.
inherit features_check
REQUIRED_DISTRO_FEATURES = "x11"

# Must match the login user created via extrausers in boat-image.bb.
BOAT_HMI_USER ?= "boat"
BOAT_HMI_UID ?= "2000"

# X keyboard layout for the helm display (an XkbLayout name, e.g. "fi",
# "us", "de"). Carries over the keymap the Weston-era weston-init.bbappend
# used to set; unlike weston.ini this is read by Xorg at server start, so
# changing it on a running device needs the session restarted.
BOAT_HMI_XKB_LAYOUT ?= "fi"

do_install() {
    install -d ${D}${systemd_system_unitdir}/getty@tty1.service.d
    install -m 0644 ${WORKDIR}/autologin.conf \
        ${D}${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf
    sed -i -e 's,@BOAT_HMI_USER@,${BOAT_HMI_USER},g' \
        ${D}${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf

    install -d ${D}${sysconfdir}/profile.d
    install -m 0755 ${WORKDIR}/boat-xfce-autostart.sh \
        ${D}${sysconfdir}/profile.d/boat-xfce-autostart.sh
    sed -i -e 's,@BOAT_HMI_UID@,${BOAT_HMI_UID},g' \
        ${D}${sysconfdir}/profile.d/boat-xfce-autostart.sh

    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-xfce-session ${D}${bindir}/boat-xfce-session

    install -d ${D}${sysconfdir}/X11/xorg.conf.d
    install -m 0644 ${WORKDIR}/10-boat-keyboard.conf \
        ${D}${sysconfdir}/X11/xorg.conf.d/10-boat-keyboard.conf
    sed -i -e 's,@BOAT_HMI_XKB_LAYOUT@,${BOAT_HMI_XKB_LAYOUT},g' \
        ${D}${sysconfdir}/X11/xorg.conf.d/10-boat-keyboard.conf
}

FILES:${PN} = "\
    ${systemd_system_unitdir}/getty@tty1.service.d/autologin.conf \
    ${sysconfdir}/profile.d/boat-xfce-autostart.sh \
    ${sysconfdir}/X11/xorg.conf.d/10-boat-keyboard.conf \
    ${bindir}/boat-xfce-session \
"

# What the two scripts above actually exec: startx (xinit), xhost, the XFCE
# session manager, and dbus-run-session (in the "dbus" package - poky's dbus
# also RPROVIDES the "dbus-x11" name xfce4-session itself asks for).
# The rest of the desktop comes from packagegroup-boat-hmi.
RDEPENDS:${PN} = "\
    xinit \
    xhost \
    dbus \
    xfce4-session \
"
