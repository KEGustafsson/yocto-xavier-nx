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

# Take the build-directory lock BEFORE touching anything in it, and keep it
# for the whole build: the cleanup below removes a leftover bitbake socket,
# and a build started concurrently from another terminal could otherwise bind
# that path in between the check and the removal - losing its own socket. The
# lock is held on a file descriptor, so it lasts until this script exits and
# covers the bitbake run below without any further bookkeeping. See
# acquire_build_lock in lib.sh.
acquire_build_lock "${BUILD_DIR}"

# Clear out any bitbake server left running by an interrupted earlier build
# before starting this one - otherwise bitbake spends its whole retry budget
# failing to connect and dies with "Busy (buildTargets in progress)", which
# reads like a build failure but isn't one. See kill_stale_bitbake in lib.sh
# (and BOAT_KEEP_BITBAKE_SERVER=1 to opt out). Deliberately placed after the
# PYTHONPATH export above: the graceful path calls `bitbake -m`, which needs
# the same Python workaround as any other bitbake invocation on this host.
kill_stale_bitbake "${BUILD_DIR}"

# 9>&- closes the lock file descriptor for bitbake and everything it forks -
# CONFIRMED NECESSARY, and not a detail: flock(2) locks belong to the open
# file description, which fork()/exec() share. bitbake leaves a memory-
# resident cooker server running after the client exits, so a server that
# inherited fd 9 would go on holding this build directory's lock with no
# script left to release it, and the NEXT build would wait for it forever.
# The lock still covers this build: it is held by this shell, which does not
# exit until bitbake returns.
bitbake "${TARGET}" 9>&-

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
