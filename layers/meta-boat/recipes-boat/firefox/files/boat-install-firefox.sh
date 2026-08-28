#!/bin/sh
# Download and install Mozilla's official Firefox for the boat's desktop.
#
#   boat-install-firefox              # report installed vs latest, change nothing
#   boat-install-firefox --install    # download, verify and install/upgrade
#   boat-install-firefox --version 154.0.1
#   boat-install-firefox --remove
#
# WHY A DOWNLOADER AND NOT A RECIPE
# Firefox is not packaged for Yocto in any layer this project fetches. The one
# that exists - meta-browser's meta-firefox - still ships only 68.9.0esr on
# kirkstone, which Mozilla stopped supporting in August 2020, and it wants
# meta-clang plus python2.7 on the build host. Shipping a browser with six
# years of unpatched CVEs onto a boat that sits on marina wifi is the wrong
# trade, and building a current Firefox from source on a kirkstone-era
# snapshot is hours of Rust-toolchain archaeology.
#
# Mozilla publishes official, current aarch64 Linux builds - the same binaries
# they ship to any Linux desktop - so fetching one is both simpler and safer
# than either. CONFIRMED: every shared library these binaries need
# (gtk3/gdk, pango, cairo, atk, gdk-pixbuf, fontconfig, freetype, alsa,
# dbus-1, libstdc++ and the X11 set) is ALREADY in this image; readelf'ing
# firefox and libxul.so against the built rootfs found nothing missing, so
# this needs no extra packages. Everything else - NSS, NSPR, sqlite - is
# bundled inside the tarball.
#
# The trade being made, stated plainly: the browser is a prebuilt binary that
# Yocto did not build, so it is outside the image's reproducibility and
# license manifests, and installing it needs working DNS and outbound HTTPS.
# In exchange the boat gets a Firefox that is current on the day you run this,
# and can be brought up to date later by running it again.
#
# WHAT THE CHECKSUM BELOW IS, AND IS NOT
# It fetches SHA256SUMS from the SAME host over the SAME TLS connection as the
# tarball. That catches a truncated or corrupted download, and a CDN edge
# serving one stale file - it does NOT catch anyone who can serve content for
# that host, because they can substitute both files consistently. Mozilla also
# publishes SHA256SUMS.asc, signed with the Mozilla Software Release key; a
# gpgv check against a pubkey shipped in this recipe is what would make this
# an authenticity check rather than an integrity check, and is the obvious
# next step. Until then: the trust anchor here is TLS to mozilla.net, nothing
# more, and this comment says so rather than letting "verified" imply more
# than it delivers.
set -eu

SELF=boat-install-firefox

# Mozilla's "latest" redirector. os=linux64-aarch64 is the arm64 Linux build;
# note it is NOT "linux-aarch64", which 404s.
BASE_URL="https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64-aarch64&lang=en-US"
CDN="https://download-installer.cdn.mozilla.net/pub/firefox/releases"

PREFIX=/opt/firefox
BINLINK=/usr/local/bin/firefox
DESKTOP=/usr/share/applications/firefox.desktop

MODE=status
WANT_VERSION=""

say() { echo "${SELF}: $*"; }
die() { echo "${SELF}: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: boat-install-firefox [options]

Install or update Mozilla's official Firefox (aarch64) for the XFCE desktop.
Firefox is not built into this image; this fetches a current build instead of
the six-year-EOL one the only available Yocto layer carries.

  -s, --status       report installed and latest versions, change nothing
                     (the default)
  -i, --install      download, checksum-verify and install or upgrade
  -V, --version VER  install this exact version instead of the latest
  -r, --remove       uninstall
  -h, --help         this text

Installs to /opt/firefox, links /usr/local/bin/firefox, and adds a menu
entry. Needs root for anything except --status. Profiles live in the calling
user's ~/.mozilla and are untouched by upgrades and by --remove.
USAGE
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -s|--status)  MODE=status ;;
        -i|--install) MODE=install ;;
        -r|--remove)  MODE=remove ;;
        -V|--version) [ "$#" -ge 2 ] || die "--version needs a value"
                      # Goes straight into a URL path below, so keep it to what
                      # a Mozilla release string can contain - "../.." would
                      # otherwise walk out of /pub/firefox/releases.
                      case "$2" in
                          ''|*[!0-9.abcdefghijklmnopqrstuvwxyz]*)
                              die "not a Firefox version: '$2' (expected e.g. 154.0.1 or 128.5.0esr)" ;;
                      esac
                      WANT_VERSION="$2"; shift ;;
        -h|--help)    usage 0 ;;
        *)            echo "${SELF}: unknown argument '$1'" >&2; usage 1 ;;
    esac
    shift
done

need() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' not found - install $2"
}

installed_version() {
    if [ -x "${PREFIX}/firefox" ]; then
        # application.ini is read without running the binary: this script may
        # well be invoked over SSH with no DISPLAY, and `firefox --version`
        # would still try to talk to X on some builds.
        sed -n 's/^Version=//p' "${PREFIX}/application.ini" 2>/dev/null | head -n1
    fi
}

# Resolve the redirector to a concrete version. Mozilla's URL carries it as
# .../releases/<version>/linux-aarch64/... which is the only place the latest
# version number is exposed without an API call.
# Shared curl options. Without a timeout, a marina wifi that associates and
# then blackholes (a captive portal, a weak link) leaves this hanging with no
# output at all - the operator sees a wedged terminal, not "no network".
# --proto restricts the protocols of the transfer itself; --proto-redir is a
# SEPARATE option covering where a redirect may go, and every fetch here uses
# -L. Both are needed: without --proto-redir a 301 to http:// is still
# followed.
CURL_OPTS="--proto =https --proto-redir =https --tlsv1.2 --connect-timeout 15 --retry 3 --retry-delay 5"

latest_version() {
    # shellcheck disable=SC2086 # CURL_OPTS is a deliberately split option list
    curl -sIL $CURL_OPTS --max-time 60 -o /dev/null -w '%{url_effective}' "$BASE_URL" 2>/dev/null \
        | sed -n 's|.*/releases/\([^/]*\)/.*|\1|p'
}

CURRENT="$(installed_version || true)"

case "$MODE" in
remove)
    [ "$(id -u)" -eq 0 ] || die "must run as root (try: sudo ${SELF} --remove)"
    [ -n "$CURRENT" ] || say "nothing installed at ${PREFIX}"
    rm -rf "$PREFIX" "$BINLINK" "$DESKTOP"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database /usr/share/applications 2>/dev/null || :
    say "removed. User profiles under ~/.mozilla were left alone."
    exit 0
    ;;
esac

need curl curl
LATEST="${WANT_VERSION:-$(latest_version)}"
[ -n "$LATEST" ] || die "could not determine the latest version - is there network/DNS?"

say "installed: ${CURRENT:-<none>}"
say "target:    ${LATEST}"

if [ "$MODE" = "status" ]; then
    if [ "$CURRENT" = "$LATEST" ]; then
        say "up to date"
    elif [ -z "$CURRENT" ]; then
        say "not installed - run: sudo ${SELF} --install"
    else
        say "an update is available - run: sudo ${SELF} --install"
    fi
    exit 0
fi

# --- install ---------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "must run as root (try: sudo ${SELF} --install)"
need tar tar
need sha256sum coreutils
need xz xz

if [ "$CURRENT" = "$LATEST" ]; then
    say "${LATEST} is already installed - nothing to do"
    exit 0
fi

TARBALL="firefox-${LATEST}.tar.xz"
URL="${CDN}/${LATEST}/linux-aarch64/en-US/${TARBALL}"
WORK="$(mktemp -d /var/tmp/boat-firefox.XXXXXX)"
# Leave nothing behind on any exit path, including a failed download - and put
# back the previous install if the swap below did not complete. Without that
# rollback, a `mv` that fails (ENOSPC on the 16 GiB rootfs is realistic) leaves
# NO /opt/firefox at all while the working copy sits unmentioned at
# /opt/firefox.old - which the next run then starts by deleting.
cleanup() {
    rm -rf "$WORK"
    if [ -d "${PREFIX}.old" ] && [ ! -d "$PREFIX" ]; then
        mv "${PREFIX}.old" "$PREFIX" 2>/dev/null \
            && echo "${SELF}: install failed - restored the previous Firefox" >&2
    fi
}
# INT/TERM need their own handlers that EXIT: a POSIX shell RESUMES at the
# interrupted point after a trap handler that does not exit, so Ctrl-C during
# the download would remove $WORK and then fall into the checksum step against
# a file that no longer exists.
trap 'cleanup' EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

say "downloading ${TARBALL}"
# shellcheck disable=SC2086 # CURL_OPTS is a deliberately split option list
curl -fL $CURL_OPTS --max-time 1800 --progress-bar -o "${WORK}/${TARBALL}" "$URL" \
    || die "download failed: $URL"

# Verify before unpacking, not after: this is a binary that will run as the
# desktop user on a machine that steers a boat. Mozilla publishes SHA256SUMS
# per release over the same HTTPS host.
say "verifying checksum against Mozilla's SHA256SUMS"
# shellcheck disable=SC2086 # CURL_OPTS is a deliberately split option list
EXPECTED="$(curl -fsL $CURL_OPTS --max-time 120 "${CDN}/${LATEST}/SHA256SUMS" 2>/dev/null \
    | awk -v p="linux-aarch64/en-US/${TARBALL}" '$2 == p {print $1}')"
[ -n "$EXPECTED" ] || die "no SHA256SUMS entry for ${TARBALL} - refusing to install"
ACTUAL="$(sha256sum "${WORK}/${TARBALL}" | cut -d' ' -f1)"
[ "$EXPECTED" = "$ACTUAL" ] || die "checksum MISMATCH - expected ${EXPECTED}, got ${ACTUAL}"
say "checksum ok"

say "unpacking"
tar -C "$WORK" -xf "${WORK}/${TARBALL}"
[ -x "${WORK}/firefox/firefox" ] || die "unexpected tarball layout - no firefox/firefox"

# Mozilla's built-in updater cannot write to /opt as the desktop user, so
# left enabled it just nags on every start about an update it can never
# apply. This script is the update mechanism; say so to Firefox.
mkdir -p "${WORK}/firefox/distribution"
cat > "${WORK}/firefox/distribution/policies.json" <<'POLICY'
{
  "policies": {
    "DisableAppUpdate": true
  }
}
POLICY

# Swap into place rather than extracting over a running install: replacing
# libxul.so under a live process is how you get a browser that segfaults on
# its next tab.
say "installing to ${PREFIX}"
# /opt exists on this image (the BSP mounts /opt/nvidia/esp) but not on every
# system this script might be dropped onto, and `mv` will not create a
# missing parent - it fails with a bare ENOENT that reads like the source is
# missing rather than the destination's directory.
mkdir -p "$(dirname "$PREFIX")"
rm -rf "${PREFIX}.old"
[ -d "$PREFIX" ] && mv "$PREFIX" "${PREFIX}.old"
# If this fails, the EXIT trap puts ${PREFIX}.old back - which is why .old is
# only removed AFTER the new tree is confirmed in place, not before the move.
mv "${WORK}/firefox" "$PREFIX" || die "could not move the new Firefox into ${PREFIX}"
[ -x "${PREFIX}/firefox" ] || die "${PREFIX}/firefox is not there after the move"
rm -rf "${PREFIX}.old"

mkdir -p "$(dirname "$BINLINK")"
ln -sfn "${PREFIX}/firefox" "$BINLINK"

# Menu entry for XFCE. StartupWMClass matches what Firefox actually sets, so
# the panel groups its windows under this launcher instead of a stray icon.
cat > "$DESKTOP" <<DESKTOPEOF
[Desktop Entry]
Type=Application
Name=Firefox
GenericName=Web Browser
Comment=Browse the web
Exec=${PREFIX}/firefox %u
Icon=${PREFIX}/browser/chrome/icons/default/default128.png
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;text/xml;application/xhtml+xml;x-scheme-handler/http;x-scheme-handler/https;
StartupNotify=true
StartupWMClass=firefox
DESKTOPEOF

command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database /usr/share/applications 2>/dev/null || :

say "installed Firefox ${LATEST}"
say "launch it from the XFCE menu, or run: firefox"
