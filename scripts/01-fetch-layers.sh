#!/usr/bin/env bash
# Clone (or update) all the layers needed for an Xavier NX / NVMe build,
# pinned to the branch defined in env.sh (default: kirkstone).
#
# Layers:
#   poky                -> OpenEmbedded-Core + bitbake (the build system)
#   meta-openembedded   -> meta-oe / meta-python / meta-networking / meta-filesystems
#                          (provides gpsd, can-utils, mosquitto, chrony, nodejs, ...)
#   meta-tegra          -> the NVIDIA Jetson BSP (kernel, bootloader, tegraflash)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

need git
mkdir -p "${LAYERS_DIR}"

# repo_name  git_url
LAYERS=(
  "poky|https://git.yoctoproject.org/poky"
  "meta-openembedded|https://github.com/openembedded/meta-openembedded.git"
  "meta-tegra|https://github.com/OE4T/meta-tegra.git"
)

clone_or_update() {
  local name="$1" url="$2" dst="${LAYERS_DIR}/$1"
  if [[ -d "${dst}/.git" ]]; then
    log "Updating ${name} (${YOCTO_BRANCH})..."
    git -C "${dst}" fetch --depth 1 origin "${YOCTO_BRANCH}"
    git -C "${dst}" checkout -q "${YOCTO_BRANCH}"
    git -C "${dst}" reset --hard -q "origin/${YOCTO_BRANCH}"
  else
    log "Cloning ${name} (${YOCTO_BRANCH})..."
    git clone --depth 1 -b "${YOCTO_BRANCH}" "${url}" "${dst}"
  fi
  log "  ${name} @ $(git -C "${dst}" rev-parse --short HEAD)"
}

for entry in "${LAYERS[@]}"; do
  clone_or_update "${entry%%|*}" "${entry##*|}"
done

log "All layers present under ${LAYERS_DIR}"
log "Pinned to branch: ${YOCTO_BRANCH}"
warn "For a reproducible product build, replace the branch checkouts above with"
warn "fixed tags/commits once you have a combination that works for you."
