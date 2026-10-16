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

# The fleet's update channel, built into /etc/boat-update/channel.conf, so a
# board is back on it after a USB reflash has emptied /data (the operator's
# /data/boat-update/channel.conf still overrides it). Set them in local.conf
# or the kas configuration; an empty URL ships no channel. A BOAT_BENCH
# image follows the bench channel by default: its bundles go only there,
# and it refuses every stable index (another compatible) as an alert.
BOAT_UPDATE_URL ??= ""
BOAT_UPDATE_CHANNEL ??= "${@'bench' if d.getVar('BOAT_BENCH') == '1' else 'stable'}"

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

    # A URL with no user name or password in it (they would be readable by
    # every local user), and nothing sed or the shell would read as syntax.
    case "${BOAT_UPDATE_URL}" in
        ""|https://*) ;;
        *) bbfatal "BOAT_UPDATE_URL must be an https:// URL or empty, got '${BOAT_UPDATE_URL}'" ;;
    esac
    case "${BOAT_UPDATE_URL}" in
        *[!A-Za-z0-9._~:/%-]*) bbfatal "BOAT_UPDATE_URL may hold only letters, digits and ._~:/%- (no user name or password), got '${BOAT_UPDATE_URL}'" ;;
    esac
    case "${BOAT_UPDATE_CHANNEL}" in
        [a-z]*) ;;
        *) bbfatal "BOAT_UPDATE_CHANNEL must be a channel name like stable, got '${BOAT_UPDATE_CHANNEL}'" ;;
    esac
    case "${BOAT_UPDATE_CHANNEL}" in
        *[!a-z0-9-]*) bbfatal "BOAT_UPDATE_CHANNEL must be a channel name like stable, got '${BOAT_UPDATE_CHANNEL}'" ;;
    esac
    install -d ${D}${sysconfdir}/boat-update
    sed -e 's|@BOAT_UPDATE_URL@|${BOAT_UPDATE_URL}|' \
        -e 's|@BOAT_UPDATE_CHANNEL@|${BOAT_UPDATE_CHANNEL}|' \
        ${WORKDIR}/channel.conf > ${D}${sysconfdir}/boat-update/channel.conf
    chmod 0644 ${D}${sysconfdir}/boat-update/channel.conf
    if grep -q '@[A-Z_]*@' ${D}${sysconfdir}/boat-update/channel.conf; then
        bbfatal "channel.conf still has a placeholder"
    fi

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-update-check.service ${WORKDIR}/boat-update-check.timer \
        ${D}${systemd_system_unitdir}/

    install -d ${D}${sysconfdir}/NetworkManager/dispatcher.d
    install -m 0755 ${WORKDIR}/90-boat-update ${D}${sysconfdir}/NetworkManager/dispatcher.d/90-boat-update
    install -d ${D}${systemd_unitdir}/system-sleep
    install -m 0755 ${WORKDIR}/boat-update-sleep ${D}${systemd_unitdir}/system-sleep/boat-update
}
do_install[vardeps] += "BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B BOAT_AB_PARTNUM_DATA \
                        BOAT_UPDATE_URL BOAT_UPDATE_CHANNEL"

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
# python3-netserver (socketserver) and python3-html: http.server, for the
# stream relay. boat-image has them only through docker-compose's requests;
# boat-update ships in every A/B image.
# e2fsprogs: debugfs, which reads a flashed slot's release without mounting
# it. systemd-extra-utils: systemd-run (the install unit, the clock retry)
# and systemd-inhibit (sleep and shutdown held off during an install) -
# otherwise only a recommendation of systemd's.
RDEPENDS:${PN} = "\
    python3-core \
    python3-json \
    python3-netserver \
    python3-html \
    python3-crypt \
    python3-fcntl \
    python3-shell \
    python3-io \
    curl \
    openssl-bin \
    rauc \
    grub-editenv \
    util-linux-blkid \
    e2fsprogs \
    systemd-extra-utils \
    iproute2-ip \
    boat-ab-boot \
"
# nmcli: the metered check. Without NetworkManager every link counts as
# metered, which is the safe answer.
# chrony and chronyc (a package of its own, which chrony does not pull in):
# the clock gate asks chronyc. Without it, timedatectl's NTPSynchronized,
# which chrony sets only with 'rtcsync' in chrony.conf (the default).
RRECOMMENDS:${PN} = "networkmanager-nmcli chrony chronyc"
