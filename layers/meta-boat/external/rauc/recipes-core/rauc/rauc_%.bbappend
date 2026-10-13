FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# The boat computer's RAUC configuration (docs/plans/ab-os-updates.md 5.6,
# 5.7): system.conf generated from the A/B layout's partition numbers, the
# keyring from BOAT_KEYS_DIR, a root-only D-Bus policy, and rauc.service
# waiting for /data.
SRC_URI += "\
    file://system.conf.in \
    file://de.pengutronix.rauc.conf \
    file://10-boat-ab-rauc.conf \
"

# The CA certificate from scripts/gen-update-keys.sh - never a file in this
# repository. It is not fetched: the fetcher unpacks an absolute file:// path
# to WORKDIR/<that path>, where meta-rauc's do_install does not look. Instead
# do_install copies it to WORKDIR/ca.cert.pem first, and its checksum is part
# of do_install's signature. An empty URI also means meta-rauc's example
# certificate can never stand in for it.
RAUC_KEYRING_FILE = "ca.cert.pem"
RAUC_KEYRING_URI = ""

python boat_check_keyring() {
    import os
    keys = d.getVar('BOAT_KEYS_DIR')
    if not keys or not os.path.isfile(os.path.join(keys, 'ca.cert.pem')):
        bb.fatal("RAUC needs the update CA certificate: BOAT_KEYS_DIR (%r) has no "
                 "ca.cert.pem. Run scripts/gen-update-keys.sh once, keep the "
                 "directory safe, and point BOAT_KEYS_DIR at it (scripts/env.sh)." % keys)
}
do_install[prefuncs] += "boat_check_keyring"
do_install[file-checksums] += "${BOAT_KEYS_DIR}/ca.cert.pem:True"

# meta-rauc's mark-good unit commits a slot as soon as multi-user.target is
# reached; on this board boat-commit's health gate decides that instead.
RRECOMMENDS:${PN}:remove = "${PN}-mark-good"

# The unprivileged user RAUC's streaming client runs as ([streaming]
# sandbox-user).
inherit useradd
USERADD_PACKAGES = "${PN}"
USERADD_PARAM:${PN} = "--system --no-create-home --home-dir /nonexistent \
    --shell /sbin/nologin --user-group rauc-stream"

do_install:prepend() {
    install -m 0644 ${BOAT_KEYS_DIR}/ca.cert.pem ${WORKDIR}/ca.cert.pem
    min_line=""
    if [ -n "${BOAT_MIN_BUNDLE_VERSION}" ]; then
        min_line="min-bundle-version=${BOAT_MIN_BUNDLE_VERSION}"
    fi
    sed -e 's|@COMPATIBLE@|${BOAT_BUNDLE_COMPATIBLE}|g' \
        -e 's|@DISK@|${BOAT_AB_DISK}|g' \
        -e 's|@PARTNUM_A@|${BOAT_AB_PARTNUM_APP_A}|g' \
        -e 's|@PARTNUM_B@|${BOAT_AB_PARTNUM_APP_B}|g' \
        -e "s|@MIN_BUNDLE_VERSION@|$min_line|g" \
        ${WORKDIR}/system.conf.in > ${WORKDIR}/system.conf
    if grep -q '@[A-Z_]*@' ${WORKDIR}/system.conf; then
        bbfatal "system.conf still has a placeholder: $(grep '@[A-Z_]*@' ${WORKDIR}/system.conf)"
    fi
}
do_install[vardeps] += "BOAT_BUNDLE_COMPATIBLE BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A \
    BOAT_AB_PARTNUM_APP_B BOAT_MIN_BUNDLE_VERSION"

do_install:append() {
    if [ -f ${D}${datadir}/dbus-1/system.d/de.pengutronix.rauc.conf ]; then
        install -m 0644 ${WORKDIR}/de.pengutronix.rauc.conf \
            ${D}${datadir}/dbus-1/system.d/de.pengutronix.rauc.conf
    fi
    install -d ${D}${systemd_system_unitdir}/rauc.service.d
    install -m 0644 ${WORKDIR}/10-boat-ab-rauc.conf \
        ${D}${systemd_system_unitdir}/rauc.service.d/10-boat-ab-rauc.conf
}

FILES:${PN}-service += "${systemd_system_unitdir}/rauc.service.d"
