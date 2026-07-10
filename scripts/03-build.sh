#!/usr/bin/env bash
# Build the target image. Produces, among other artifacts, the flashing
# tarball:  <IMAGE>-<MACHINE>.tegraflash.tar.gz  in tmp/deploy/images/<MACHINE>/
#
# Usage:   scripts/03-build.sh [image-name]
#          IMAGE=boat-image scripts/03-build.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

TARGET="${1:-${IMAGE}}"
[[ -f "${BUILD_DIR}/conf/local.conf" ]] || die "build not configured; run scripts/02-configure-build.sh first"

[[ "$(id -u)" -ne 0 ]] || die "do NOT run bitbake as root"

log "Building '${TARGET}' for MACHINE=${MACHINE} (branch ${YOCTO_BRANCH})"
warn "First build downloads many GB of source and can take 2-6 h on a fast host."

# oe-init-build-env re-uses the existing conf, then hand off to bitbake.
# shellcheck disable=SC1091
source "${LAYERS_DIR}/poky/oe-init-build-env" "${BUILD_DIR}" >/dev/null

bitbake "${TARGET}"

DEPLOY="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
log "Build finished. Artifacts in: ${DEPLOY}"
if ls "${DEPLOY}/${TARGET}-${MACHINE}.tegraflash.tar.gz" >/dev/null 2>&1; then
  log "Flashing tarball: ${DEPLOY}/${TARGET}-${MACHINE}.tegraflash.tar.gz"
else
  warn "No .tegraflash.tar.gz found - check that MACHINE is a Tegra machine."
fi
log "Next: scripts/04-unpack-tegraflash.sh"
