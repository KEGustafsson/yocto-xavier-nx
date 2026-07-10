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

log "Installing Yocto build-host packages (sudo required)..."
sudo apt-get update
# Yocto kirkstone 'Required Packages for the Build Host' (Ubuntu) + extras.
sudo apt-get install -y \
  gawk wget git diffstat unzip texinfo gcc build-essential chrpath socat \
  cpio python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping \
  python3-git python3-jinja2 python3-subunit zstd lz4 file locales \
  libacl1 mesa-common-dev

log "Installing flashing host packages..."
# tegraflash/initrd-flash need these on the host that talks to the board.
sudo apt-get install -y \
  sudo usbutils lbzip2 python3-yaml libxml2-utils zstd

log "Ensuring en_US.UTF-8 locale (required by bitbake)..."
sudo locale-gen en_US.UTF-8 || true

if command -v tlp >/dev/null 2>&1; then
  warn "TLP is installed; it can interrupt USB during flashing."
  warn "Consider: sudo apt remove tlp && reboot   before flashing."
fi

log "Host dependencies installed."
log "Reminder: do NOT run bitbake as root. Use a normal user account."
