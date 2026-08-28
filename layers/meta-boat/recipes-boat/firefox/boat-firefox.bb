SUMMARY = "Installer for Mozilla's official Firefox on the boat's desktop"
DESCRIPTION = "Ships boat-install-firefox, which downloads, checksum-verifies \
and installs Mozilla's official aarch64 Firefox build into /opt/firefox with \
an XFCE menu entry. Firefox is not packaged for Yocto in any layer this \
project fetches - meta-browser's meta-firefox still carries only 68.9.0esr on \
kirkstone, EOL since 2020 - so the browser is fetched at runtime rather than \
built, and re-running the command upgrades it. See \
docs/05-phase2-boat-computer-layer.md 'A browser on the boat's own screen'."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://boat-install-firefox.sh"

S = "${WORKDIR}"

inherit allarch

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/boat-install-firefox.sh \
        ${D}${bindir}/boat-install-firefox
}

FILES:${PN} = "${bindir}/boat-install-firefox"

# Deliberately not run at build or first boot. do_rootfs has no business
# reaching out to the internet - it would make the image non-reproducible and
# put a binary Yocto never built inside the license manifest - and a first-boot
# unit would block on DNS on a boat that may have no uplink for weeks. The
# operator runs it once, when there is a connection.
#
# NOTE this means the browser is NOT in the flashed image: `boat-install-firefox
# --install` has to be run on the device before there is a Firefox to launch.
#
# What the script actually calls. curl/tar/xz/sha256sum are all present in any
# image built from packagegroup-boat (curl via -nettools, the rest from
# busybox/coreutils and the base), but this package is useless without them,
# so name them rather than rely on the packagegroup being installed.
# update-desktop-database is optional - the script skips it if missing - and
# is therefore not listed.
#
# NOT listed either: the shared libraries the downloaded Firefox links
# against. CONFIRMED by readelf'ing firefox and libxul.so from Mozilla's
# aarch64 tarball against the built rootfs - gtk+3, pango, cairo, atk,
# gdk-pixbuf, fontconfig, freetype, alsa-lib, dbus and the X11 set are all
# already pulled in by packagegroup-boat-hmi's desktop, and NSS/NSPR/sqlite
# are bundled inside the tarball itself. Nothing extra is needed; if that ever
# changes, the symptom is the binary failing at exec with a missing .so.
# ca-certificates is not optional here: every URL the script fetches is https,
# and curl verifies the peer against the trust store by default - with none
# installed, every download fails at the TLS handshake. (The script's --proto /
# --proto-redir pinning is a separate thing: it stops a redirect leaving https,
# it has nothing to do with certificate verification.) It is in
# packagegroup-boat-containers too, but this package must not depend on that
# packagegroup being installed.
RDEPENDS:${PN} = "\
    curl \
    ca-certificates \
    tar \
    xz \
    coreutils \
"
