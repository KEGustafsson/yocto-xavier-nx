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

# The keyring goes into every image, world-readable, and into sstate and the
# deploy directory: it must hold CA certificates and nothing else. A keyring
# put together by hand for a CA rotation (plan 5.7) next to ca.key.pem is
# where a private key would slip in - and anyone holding it could sign for
# the whole fleet. Returns the certificates, as DER.
def boat_keyring_certs(path, what):
    import base64, re
    with open(path) as f:
        text = f.read()
    if 'PRIVATE KEY' in text:
        bb.fatal("%s (%s) holds a PRIVATE KEY: it would ship on every board. It must hold only "
                 "CA certificates - rebuild it from the ca.cert.pem files alone" % (what, path))
    certs = []
    for kind, body in re.findall(r'-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----', text, re.S):
        if kind != 'CERTIFICATE':
            bb.fatal("%s (%s) holds a %s block: it must hold only CA certificates" % (what, path, kind))
        der = base64.b64decode(''.join(body.split()))
        # basicConstraints (2.5.29.19): [critical,] OCTET STRING { SEQUENCE { cA TRUE ... } }
        i = der.find(bytes.fromhex('0603551d13'))
        rest = der[i + 5:] if i >= 0 else b''
        if rest[:3] == bytes.fromhex('0101ff'):
            rest = rest[3:]
        if not (len(rest) > 7 and rest[0] == 0x04 and rest[2] == 0x30 and rest[4:7] == bytes.fromhex('0101ff')):
            bb.fatal("%s (%s) holds a certificate that is not a CA (no basicConstraints CA:TRUE): "
                     "the keyring is the update CA's certificate(s) only" % (what, path))
        certs.append(der)
    if not certs:
        bb.fatal("%s (%s) holds no certificate" % (what, path))
    return certs

python boat_check_keyring() {
    import os
    keys = d.getVar('BOAT_KEYS_DIR')
    if not keys or not os.path.isfile(os.path.join(keys, 'ca.cert.pem')):
        bb.fatal("RAUC needs the update CA certificate: BOAT_KEYS_DIR (%r) has no "
                 "ca.cert.pem. Run scripts/gen-update-keys.sh once, keep the "
                 "directory safe, and point BOAT_KEYS_DIR at it (scripts/env.sh)." % keys)
    fleet = boat_keyring_certs(os.path.join(keys, 'ca.cert.pem'), "the update keyring")
    bench = d.getVar('BOAT_BENCH_KEYS_DIR')
    if d.getVar('BOAT_BENCH') == '1':
        if not bench or not os.path.isfile(os.path.join(bench, 'ca.cert.pem')):
            bb.fatal("BOAT_BENCH = \"1\" needs the bench CA: BOAT_BENCH_KEYS_DIR (%r) has no "
                     "ca.cert.pem. Run scripts/gen-update-keys.sh --bench." % bench)
        # Compared as certificates, not bytes: a re-encoded copy is the same CA.
        bench_certs = boat_keyring_certs(os.path.join(bench, 'ca.cert.pem'), "the bench keyring")
        if any(c in fleet for c in bench_certs):
            bb.fatal("BOAT_BENCH_KEYS_DIR (%r) holds the fleet CA: the bench needs its own "
                     "(scripts/gen-update-keys.sh --bench), or its fault bundles would be "
                     "trusted by every boat." % bench)
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

# The update settings, checked as the build parses - kas builds pass them
# straight in, past scripts/02-configure-build.sh's own checks:
# - BOAT_RELEASE a version RAUC and boat-update can compare (a board whose
#   release is not one cannot tell an older offer from a newer one);
# - BOAT_KEYS_DIR absolute, and not inside this repository or the build
#   directory (a clean-up deletes it, git commits it);
# - min-bundle-version a version too, and not above this build's own
#   release - or every board it is flashed on would refuse this build's own
#   bundles.
BOAT_RAUC_APPEND_DIR := "${THISDIR}"
def boat_semver_le(a, b):
    """RAUC's r_semver_less_equal(a, b); ValueError if either does not parse."""
    def parse(v):
        v = v.rsplit('+', 1)[0] if '+' in v else v
        pre = None
        if '-' in v:
            v, pre = v.rsplit('-', 1)
        parts = v.split('.', 2)
        if not all(p.isdigit() for p in parts):
            raise ValueError(v)
        return [int(p) for p in parts] + [0] * (3 - len(parts)), pre
    (ca, pa), (cb, pb) = parse(a), parse(b)
    if ca != cb:
        return ca < cb
    if pa is None or pb is None:
        return pb is None
    fa, fb = pa.split('.') if pa else [], pb.split('.') if pb else []
    for x, y in zip(fa, fb):
        if x.isdigit() and y.isdigit():
            if int(x) != int(y):
                return int(x) < int(y)
        elif x.isdigit() or y.isdigit():
            return x.isdigit()
        elif x != y:
            return x < y
    return len(fa) <= len(fb)

python () {
    import os, re
    release = d.getVar('BOAT_RELEASE') or ''
    if release and not re.match(r'^[0-9]+(\.[0-9]+){0,2}(-[0-9A-Za-z.]+)?(\+[0-9A-Za-z.]+)?$', release):
        bb.fatal("BOAT_RELEASE must be a version like 1.4.0 or 2026.10.1-rc.1 (no '-' inside the "
                 "pre-release part: RAUC splits at the last one), got %r" % release)
    keys = d.getVar('BOAT_KEYS_DIR') or ''
    if keys:
        if not os.path.isabs(keys):
            bb.fatal("BOAT_KEYS_DIR must be an absolute path, got %r" % keys)
        repo = os.path.realpath(os.path.join(d.getVar('BOAT_RAUC_APPEND_DIR'), *[os.pardir] * 6))
        for inside in (repo, os.path.realpath(d.getVar('TOPDIR'))):
            if (os.path.realpath(keys) + os.sep).startswith(inside + os.sep):
                bb.fatal("BOAT_KEYS_DIR (%s) is inside %s - keep the update keys where a clean-up "
                         "cannot delete them and git cannot commit them" % (keys, inside))
    if (d.getVar('BOAT_BENCH') or '0') not in ('0', '1'):
        bb.fatal("BOAT_BENCH must be 0 or 1, got %r" % d.getVar('BOAT_BENCH'))
    floor = d.getVar('BOAT_MIN_BUNDLE_VERSION') or ''
    release = d.getVar('BOAT_RELEASE_EFFECTIVE')
    if not floor:
        return
    try:
        ok = boat_semver_le(floor, release)
    except ValueError:
        bb.fatal("BOAT_MIN_BUNDLE_VERSION (%r) and BOAT_RELEASE (%r) must both be versions "
                 "like 1.4.0 or 2026.10.1-rc.1" % (floor, release))
    if not ok:
        bb.fatal("BOAT_MIN_BUNDLE_VERSION (%s) is above this build's BOAT_RELEASE (%s): "
                 "every board flashed with it would refuse its own bundles" % (floor, release))
}

# A bench image (BOAT_BENCH = "1") also trusts the bench CA, which signs the
# fault-injection bundles (boat-bundle-faulty). A boat's image never does:
# such a bundle is rejected there before anything is written (plan 5.7).
do_install:append() {
    if [ "${BOAT_BENCH}" = "1" ]; then
        cat "${BOAT_BENCH_KEYS_DIR}/ca.cert.pem" >> ${D}${RAUC_CONF_DIR}/ca.cert.pem
    fi
}
do_install[vardeps] += "BOAT_BENCH BOAT_BENCH_KEYS_DIR"
do_install[file-checksums] += "${@'%s/ca.cert.pem:True' % d.getVar('BOAT_BENCH_KEYS_DIR') if d.getVar('BOAT_BENCH') == '1' else ''}"

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
