SUMMARY = "Wake-on-LAN + remote SC7 (deep sleep) power control for the boat computer"
DESCRIPTION = "Arms magic-packet Wake-on-LAN on the Ethernet interface(s) at \
boot and around every suspend, and ships boat-sleep - a root command meant to \
be run over SSH that puts the board into SC7 (Tegra deep sleep) only once it \
has confirmed something can wake it again. See \
docs/05-phase2-boat-computer-layer.md 'Power: Wake-on-LAN and remote SC7'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://boat-wol-arm.sh \
    file://boat-wol-dispatcher.sh \
    file://boat-sleep.sh \
    file://boat-power-sleep-hook.sh \
    file://boat-wol.service \
    file://boat-power.conf \
    file://90-boat-wol.conf \
    "

S = "${WORKDIR}"

inherit systemd allarch

SYSTEMD_SERVICE:${PN} = "boat-wol.service"

# Interfaces armed for magic-packet wake. Baked into both the runtime config
# file and the NetworkManager default below, so the two can't drift.
# eth0 is the devkit's on-board RJ45; override in local.conf if a different
# port is the one reachable from the boat's network.
BOAT_WOL_INTERFACES ?= "eth0"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-wol-arm.sh ${D}${bindir}/boat-wol-arm
    install -m 0755 ${WORKDIR}/boat-sleep.sh ${D}${bindir}/boat-sleep

    install -d ${D}${sysconfdir}/default
    install -m 0644 ${WORKDIR}/boat-power.conf ${D}${sysconfdir}/default/boat-power
    sed -i -e 's,^BOAT_WOL_INTERFACES=.*,BOAT_WOL_INTERFACES="${BOAT_WOL_INTERFACES}",' \
        ${D}${sysconfdir}/default/boat-power

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-wol.service ${D}${systemd_system_unitdir}/

    # systemd-sleep runs every executable in this directory with "pre"/"post"
    # around a suspend. ${systemd_unitdir} tracks the usrmerge setting, which
    # is where systemd itself looks (/lib/systemd or /usr/lib/systemd).
    install -d ${D}${systemd_unitdir}/system-sleep
    install -m 0755 ${WORKDIR}/boat-power-sleep-hook.sh \
        ${D}${systemd_unitdir}/system-sleep/boat-power

    # NetworkManager's global connection default. match-device takes a
    # comma-separated list of specs, so turn "eth0 eth1" into
    # "interface-name:eth0,interface-name:eth1".
    match=""
    for i in ${BOAT_WOL_INTERFACES}; do
        if [ -z "$match" ]; then
            match="interface-name:$i"
        else
            match="$match,interface-name:$i"
        fi
    done
    # NM refuses to run a dispatcher script that is group- or world-writable,
    # and does so silently - hence the explicit 0755 root-owned install.
    install -d ${D}${sysconfdir}/NetworkManager/dispatcher.d
    install -m 0755 ${WORKDIR}/boat-wol-dispatcher.sh \
        ${D}${sysconfdir}/NetworkManager/dispatcher.d/90-boat-wol

    install -d ${D}${sysconfdir}/NetworkManager/conf.d
    install -m 0644 ${WORKDIR}/90-boat-wol.conf \
        ${D}${sysconfdir}/NetworkManager/conf.d/90-boat-wol.conf
    # '|' as the sed delimiter, not the ',' used elsewhere in this file: the
    # replacement is itself comma-separated once there is more than one
    # interface, and a comma delimiter turns that into "unknown option to `s'".
    sed -i -e "s|@BOAT_WOL_MATCH@|$match|" \
        ${D}${sysconfdir}/NetworkManager/conf.d/90-boat-wol.conf
}

FILES:${PN} = "\
    ${bindir}/boat-wol-arm \
    ${bindir}/boat-sleep \
    ${sysconfdir}/default/boat-power \
    ${sysconfdir}/NetworkManager/conf.d/90-boat-wol.conf \
    ${sysconfdir}/NetworkManager/dispatcher.d/90-boat-wol \
    ${systemd_system_unitdir}/boat-wol.service \
    ${systemd_unitdir}/system-sleep/boat-power \
"

# Both files are meant to be edited on the boat (interface list, the
# require-WoL interlock, the watchdog knob) - keep those edits across a
# package upgrade instead of silently reverting them.
CONFFILES:${PN} = "\
    ${sysconfdir}/default/boat-power \
    ${sysconfdir}/NetworkManager/conf.d/90-boat-wol.conf \
"

# ethtool is what actually arms and reads back the WoL flag; it is already in
# packagegroup-boat-nettools, but this package is useless without it, so say
# so rather than depending on the packagegroup happening to be installed.
# NetworkManager and watchdog are deliberately NOT RDEPENDS: the conf.d
# snippet and the watchdog knob are inert if either is absent, and this
# package should stay installable on a smaller image.
RDEPENDS:${PN} = "ethtool"
