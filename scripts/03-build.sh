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
# It isn't nounset-safe (e.g. it checks $BBSERVER with no default), so relax
# -u just for this sourced script.
# shellcheck disable=SC1091
set +u
source "${LAYERS_DIR}/poky/oe-init-build-env" "${BUILD_DIR}" >/dev/null
set -u

# Python 3.14 changed multiprocessing's default start method (fork ->
# forkserver), which breaks kirkstone-era bitbake's hashserv worker (see
# scripts/pyfix/sitecustomize.py). Force fork back via a sitecustomize hook.
# bitbake scrubs os.environ down to a small whitelist on startup
# (bb.utils.clean_environment), so PYTHONPATH must be explicitly
# whitelisted via BB_ENV_PASSTHROUGH_ADDITIONS or it never reaches the
# forked bitbake-server process.
export PYTHONPATH="${HERE}/pyfix${PYTHONPATH:+:${PYTHONPATH}}"
export BB_ENV_PASSTHROUGH_ADDITIONS="PYTHONPATH${BB_ENV_PASSTHROUGH_ADDITIONS:+ ${BB_ENV_PASSTHROUGH_ADDITIONS}}"

bitbake "${TARGET}"

DEPLOY="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
log "Build finished. Artifacts in: ${DEPLOY}"
# Match the naming accepted by 04-unpack-tegraflash.sh (both .tar.gz and .tar.zst).
TARBALL="$(ls -t \
  "${DEPLOY}/${TARGET}-${MACHINE}.tegraflash.tar.gz" \
  "${DEPLOY}/${TARGET}-${MACHINE}.tegraflash-tar.zst" \
  "${DEPLOY}"/*.tegraflash.tar.gz "${DEPLOY}"/*.tegraflash-tar.zst \
  2>/dev/null | head -n1 || true)"
if [[ -n "${TARBALL}" ]]; then
  log "Flashing tarball: ${TARBALL}"
else
  warn "No .tegraflash tarball found - check that MACHINE (${MACHINE}) is a Tegra machine."
fi
log "Next: scripts/04-unpack-tegraflash.sh"
