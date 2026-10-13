SUMMARY = "boat-update: over-the-air OS updates for the A/B layout"
DESCRIPTION = "Checks a signed update channel (index.p7, verified against \
the update CA only), downloads the offered RAUC bundle resumably and only \
over an allowed link, and installs it into the other slot when a person \
says so; also status, rollback and history. Never installs or reboots on \
its own. See docs/07-updates.md and docs/plans/ab-os-updates.md 5.10."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "\
    file://boat-update.py \
    file://channel.conf \
    file://boat-update-check.service \
    file://boat-update-check.timer \
    file://90-boat-update \
    file://boat-update-sleep \
"

S = "${WORKDIR}"

inherit allarch systemd

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${sbindir}
    sed -e 's|@DISK@|${BOAT_AB_DISK}|g' \
        -e 's|@PARTNUM_A@|${BOAT_AB_PARTNUM_APP_A}|g' \
        -e 's|@PARTNUM_B@|${BOAT_AB_PARTNUM_APP_B}|g' \
        -e 's|@PARTNUM_DATA@|${BOAT_AB_PARTNUM_DATA}|g' \
        ${WORKDIR}/boat-update.py > ${D}${sbindir}/boat-update
    chmod 0755 ${D}${sbindir}/boat-update
    if grep -q '"@[A-Z_]*@"' ${D}${sbindir}/boat-update; then
        bbfatal "boat-update still has a placeholder"
    fi

    install -d ${D}${sysconfdir}/boat-update
    install -m 0644 ${WORKDIR}/channel.conf ${D}${sysconfdir}/boat-update/channel.conf

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-update-check.service ${WORKDIR}/boat-update-check.timer \
        ${D}${systemd_system_unitdir}/

    install -d ${D}${sysconfdir}/NetworkManager/dispatcher.d
    install -m 0755 ${WORKDIR}/90-boat-update ${D}${sysconfdir}/NetworkManager/dispatcher.d/90-boat-update
    install -d ${D}${systemd_unitdir}/system-sleep
    install -m 0755 ${WORKDIR}/boat-update-sleep ${D}${systemd_unitdir}/system-sleep/boat-update
}
do_install[vardeps] += "BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B BOAT_AB_PARTNUM_DATA"

SYSTEMD_SERVICE:${PN} = "boat-update-check.timer"

FILES:${PN} = "\
    ${sbindir}/boat-update \
    ${sysconfdir}/boat-update/channel.conf \
    ${systemd_system_unitdir} \
    ${sysconfdir}/NetworkManager/dispatcher.d/90-boat-update \
    ${systemd_unitdir}/system-sleep/boat-update \
"

# The image's defaults; an update replaces them - the operator's settings
# live in /data/boat-update/channel.conf, which no update touches.
RDEPENDS:${PN} = "\
    python3-core \
    python3-json \
    python3-crypt \
    python3-fcntl \
    python3-shell \
    python3-io \
    curl \
    openssl-bin \
    rauc \
    grub-editenv \
    util-linux-blkid \
    util-linux-mount \
    iproute2-ip \
    boat-ab-boot \
"
# nmcli: the metered check. Without NetworkManager every link counts as
# metered, which is the safe answer.
RRECOMMENDS:${PN} = "networkmanager-nmcli"
