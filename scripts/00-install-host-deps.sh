#!/usr/bin/env bash
# Install the host packages needed to run a Yocto/OpenEmbedded kirkstone build
# and to flash a Jetson. Tested on Ubuntu 20.04/22.04 (x86-64).
#
# NOTE: a Yocto build MUST run on an x86-64 Linux host. NVIDIA's low-level
# flashing tools are x86-64 binaries and will not run on ARM or inside most VMs.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

[[ "$(uname -m)" == "x86_64" ]] || warn "host is $(uname -m); Yocto + tegraflash need x86-64"

if ! command -v apt-get >/dev/null 2>&1; then
  die "this helper targets Debian/Ubuntu; install the equivalent packages for your distro (see Yocto 'Required Packages for the Build Host')"
fi

# DEBIAN_FRONTEND=noninteractive throughout: a package that stops to ask about
# a service restart or a modified config turns an otherwise unattended run into
# a hang with no prompt visible in a scrollback.
export DEBIAN_FRONTEND=noninteractive

log "Installing Yocto build-host packages (sudo required)..."
sudo -E apt-get update
# Yocto kirkstone 'Required Packages for the Build Host' (Ubuntu) + extras.
sudo -E apt-get install -y \
  gawk wget git diffstat unzip texinfo gcc build-essential chrpath socat \
  cpio python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping \
  python3-git python3-jinja2 python3-subunit zstd lz4 file locales \
  libacl1 mesa-common-dev

log "Installing flashing host packages..."
# tegraflash/initrd-flash need these on the host that talks to the board.
# (`sudo` itself is deliberately not in this list - it is already being used to
# run the command.)
sudo -E apt-get install -y \
  usbutils lbzip2 python3-yaml libxml2-utils

# kirkstone's -native recipes predate GCC 13+: old gnulib (m4-native,
# unzip-native) breaks under a C23 default, K&R probes (gmp-native) fail, and
# LLVM 13 does not compile against a current libstdc++.
# scripts/02-configure-build.sh points BUILD_CC/BUILD_CXX at gcc-12/g++-12 -
# but only if they are actually present, so install them here rather than
# leaving it as a manual step the reader has to notice in docs/02. Not fatal
# if the archive does not carry them (Ubuntu 20.04): the configure script
# simply leaves BUILD_CC alone and the host's own GCC is used.
log "Installing gcc-12/g++-12 for kirkstone's -native builds..."
if ! sudo -E apt-get install -y gcc-12 g++-12; then
  warn "gcc-12/g++-12 are not available from this host's archives."
  warn "That is fine on a host whose default GCC is 12 or older (Ubuntu 22.04"
  warn "and earlier). On a newer host, -native recipes will fail to compile -"
  warn "see docs/02-host-prerequisites.md 'Newer hosts'."
fi

log "Ensuring en_US.UTF-8 locale (required by bitbake)..."
sudo locale-gen en_US.UTF-8 || true

if command -v tlp >/dev/null 2>&1; then
  warn "TLP is installed; it can interrupt USB during flashing."
  warn "Consider: sudo apt remove tlp && reboot   before flashing."
fi

log "Host dependencies installed."
log "Reminder: do NOT run bitbake as root. Use a normal user account."
