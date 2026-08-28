SUMMARY = "Bluetooth on by default, and still on after a wake from SC7"
DESCRIPTION = "Ships the /etc/bluetooth/main.conf that poky's bluez5 recipe \
does not, so bluetoothd powers each controller up as it finds it, and carries \
the pre-suspend Bluetooth power state across a suspend/resume so a radio that \
was on before a boat-sleep is on again afterwards. See \
docs/05-phase2-boat-computer-layer.md 'Bluetooth: on by default, and still on \
after a wake'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://main.conf \
    file://boat-bt-power.sh \
    file://boat-bluetooth-sleep-hook.sh \
    file://boat-bluetooth-resume.service \
    file://boat-bluetooth.conf \
    "

S = "${WORKDIR}"

inherit systemd allarch features_check

# Both halves are systemd mechanisms - a system-sleep hook and a service - and
# the whole package is pointless without a Bluetooth stack to configure. On a
# build missing either they would be installed and then silently never run,
# which for a package whose job is "the radio is on" is the worst way to fail.
# boat-power and boat-hmi-autostart set the same precedent.
REQUIRED_DISTRO_FEATURES = "systemd bluetooth"

# Deliberately empty. boat-bluetooth-resume.service has no [Install] section
# and must never be enabled: it is started by name from the system-sleep hook
# on each resume, and AutoEnable already covers boot. See the unit's own
# comment.
SYSTEMD_SERVICE:${PN} = ""

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-bt-power.sh ${D}${bindir}/boat-bt-power

    # The file bluez5 leaves out. Nothing else in the image packages
    # /etc/bluetooth/main.conf, so there is no file conflict to resolve -
    # poky's bluez5 do_install:append installs only network.conf and
    # input.conf into this directory.
    install -d ${D}${sysconfdir}/bluetooth
    install -m 0644 ${WORKDIR}/main.conf ${D}${sysconfdir}/bluetooth/main.conf

    install -d ${D}${sysconfdir}/default
    install -m 0644 ${WORKDIR}/boat-bluetooth.conf ${D}${sysconfdir}/default/boat-bluetooth

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-bluetooth-resume.service ${D}${systemd_system_unitdir}/

    # systemd-sleep runs every executable in this directory with "pre"/"post"
    # around a suspend. ${systemd_unitdir} tracks the usrmerge setting, which
    # is where systemd itself looks (/lib/systemd or /usr/lib/systemd).
    install -d ${D}${systemd_unitdir}/system-sleep
    install -m 0755 ${WORKDIR}/boat-bluetooth-sleep-hook.sh \
        ${D}${systemd_unitdir}/system-sleep/boat-bluetooth
}

FILES:${PN} = "\
    ${bindir}/boat-bt-power \
    ${sysconfdir}/bluetooth/main.conf \
    ${sysconfdir}/default/boat-bluetooth \
    ${systemd_system_unitdir}/boat-bluetooth-resume.service \
    ${systemd_unitdir}/system-sleep/boat-bluetooth \
"

# Both are meant to be edited on the boat - the AutoEnable policy itself, and
# the resume knobs - so keep those edits across a package upgrade instead of
# silently reverting them.
CONFFILES:${PN} = "\
    ${sysconfdir}/bluetooth/main.conf \
    ${sysconfdir}/default/boat-bluetooth \
"

# bluez5 is what reads main.conf and owns the adapter; dbus-tools is where
# dbus-send lives (poky splits it out of the dbus package, which ships only
# the daemon), and boat-bt-power talks to bluetoothd through nothing else.
# rfkill is RRECOMMENDS, not RDEPENDS: boat-bt-power guards every use of it
# with command -v and works without it - it just cannot clear a soft block
# for you then.
RDEPENDS:${PN} = "bluez5 dbus-tools"
RRECOMMENDS:${PN} = "util-linux-rfkill"
