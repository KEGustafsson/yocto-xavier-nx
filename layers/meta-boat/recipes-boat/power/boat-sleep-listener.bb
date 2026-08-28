SUMMARY = "Suspend the boat computer with one signed UDP packet"
DESCRIPTION = "Wake-on-LAN's missing counterpart. Waking is done by the NIC, \
which matches a magic packet while the host is in SC7; sleeping has no hardware \
equivalent, so this listens for a single UDP datagram carrying an HMAC-SHA256 \
over a timestamp and a nonce, and on a valid one runs boat-sleep. Gives the \
same fire-and-forget shape as boat-wake without an SSH session or a host key \
that changes on every reflash. See \
docs/05-phase2-boat-computer-layer.md 'Power: Wake-on-LAN and remote SC7'."
#
# CONFIRMED ON HARDWARE: on a Xavier NX running this image, a signed packet on
# UDP 9099 suspends the board to SC7, and a Wake-on-LAN magic packet brings it
# back afterwards - the full remote power cycle, not each half in isolation.
# Driven both by wol/boat-sleep-udp.sh and by clients/typescript, so the wire
# format is confirmed against a real boat-sleepd rather than only against the
# conformance vectors that stand in for it.
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://boat-sleepd.py \
    file://boat-sleep-listener.socket \
    file://boat-sleep-listener.service \
    file://boat-sleep-listener.conf \
    "

S = "${WORKDIR}"

# allarch: a Python script and three unit files, no compiled anything.
inherit systemd allarch features_check

# The whole mechanism is a socket unit and a service unit. On a sysvinit build
# they would be installed and then silently never run - for a package whose job
# is "the boat can be told to sleep from ashore", failing at parse time is far
# better than shipping something inert. boat-power sets the same precedent.
REQUIRED_DISTRO_FEATURES = "systemd"

# The SOCKET is what gets enabled, not the service: the service is
# socket-activated and pulls its own socket in via Requires=. Enabling the
# service instead would bind the port twice over on a manual start.
SYSTEMD_SERVICE:${PN} = "boat-sleep-listener.socket"

# boat-sleep is the only thing this ever runs; without it the package is inert
# (the service has a ConditionPathExists for it, so it would quietly not
# start). python3-netclient carries hmac and python3-crypt carries hashlib -
# they are NOT both in python3-core, and getting that wrong yields an
# ImportError at first packet rather than at build time.
RDEPENDS:${PN} = " \
    boat-power \
    python3-core \
    python3-crypt \
    python3-netclient \
    "

# Explicit, because the default FILES:${PN} in bitbake.conf does NOT cover
# ${systemd_system_unitdir} - it lists ${base_libdir}/*${SOLIBS} and the udev
# directories, not /lib/systemd/system. systemd.bbclass adds only the units
# named in SYSTEMD_SERVICE (plus anything they reach through Also=), which here
# is the SOCKET alone; the .service would be installed into ${D}, shipped in no
# package, and fail do_package_qa as installed-but-not-shipped. Listing both
# here keeps the service packaged without SYSTEMD_SERVICE also ENABLING it,
# which is the thing that must not happen - the socket is what gets enabled.
FILES:${PN} = "\
    ${bindir}/boat-sleepd \
    ${sysconfdir}/default/boat-sleep-listener \
    ${systemd_system_unitdir}/boat-sleep-listener.socket \
    ${systemd_system_unitdir}/boat-sleep-listener.service \
"

# Meant to be edited on the boat (the clock window, the key path) - keep those
# edits across a package upgrade instead of silently reverting them.
CONFFILES:${PN} = "${sysconfdir}/default/boat-sleep-listener"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-sleepd.py ${D}${bindir}/boat-sleepd

    install -d ${D}${sysconfdir}/default
    install -m 0644 ${WORKDIR}/boat-sleep-listener.conf \
        ${D}${sysconfdir}/default/boat-sleep-listener

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/boat-sleep-listener.socket \
        ${D}${systemd_system_unitdir}/boat-sleep-listener.socket
    install -m 0644 ${WORKDIR}/boat-sleep-listener.service \
        ${D}${systemd_system_unitdir}/boat-sleep-listener.service
}

# The key is deliberately NOT generated at build time and NOT shipped. A secret
# baked into an image is the same secret on every board built from it, which is
# no secret at all - and this one is the difference between "the crew can
# suspend the boat" and "anyone on the marina wifi can".
#
# Runs on the board on first boot, not at build time: ${D} is absent from the
# condition on purpose, so this is skipped during rootfs assembly (where it
# would bake one key into the image) and deferred to the target.
pkg_postinst_ontarget:${PN}() {
    if [ ! -e $D${sysconfdir}/boat-sleep.key ]; then
        umask 077
        if command -v openssl >/dev/null 2>&1; then
            openssl rand -hex 32 > $D${sysconfdir}/boat-sleep.key
        else
            # od over /dev/urandom: no openssl dependency for what is 32 bytes
            # of randomness. tr strips the spaces od puts between pairs.
            od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > $D${sysconfdir}/boat-sleep.key
            echo >> $D${sysconfdir}/boat-sleep.key
        fi
        chmod 0600 $D${sysconfdir}/boat-sleep.key
    fi
}
