#!/usr/bin/env bash
# Unpack the tegraflash tarball produced by the build into a clean directory
# ready for flashing. Always use 'tar' (never a GUI extractor) - the flashing
# scripts rely on exact file permissions/symlinks.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

DEPLOY="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
# IMAGE defaults to core-image-base (env.sh) unless exported - if you built a
# different IMAGE (e.g. boat-image) and forget to export it here too, this
# silently unpacks a *different, possibly stale* tarball instead of failing
# loudly. Always echo what was actually resolved so that mismatch is visible
# before you flash it.
log "IMAGE=${IMAGE} (unset it and re-export explicitly if this isn't what you built)"

# Accept either the kirkstone .tar.gz or the newer .tar.zst naming.
TARBALL=""
for cand in \
  "${DEPLOY}/${IMAGE}-${MACHINE}.tegraflash.tar.gz" \
  "${DEPLOY}/${IMAGE}-${MACHINE}.tegraflash-tar.zst"; do
  [[ -e "${cand}" ]] && TARBALL="${cand}" && break
done
# Fall back to newest matching file.
[[ -n "${TARBALL}" ]] || TARBALL="$(ls -t "${DEPLOY}"/*.tegraflash.tar.gz "${DEPLOY}"/*.tegraflash-tar.zst 2>/dev/null | head -n1 || true)"
[[ -n "${TARBALL}" ]] || die "no tegraflash tarball in ${DEPLOY}; run scripts/03-build.sh"

log "Using tarball: ${TARBALL}"

# Warn if a *newer* tarball for a *different* IMAGE exists in the same deploy
# dir - the exact trap above: IMAGE resolved to something with an existing
# tarball, but it isn't the most recently built one, so you're about to flash
# stale content without any error.
NEWEST="$(ls -t "${DEPLOY}"/*.tegraflash.tar.gz "${DEPLOY}"/*.tegraflash-tar.zst 2>/dev/null | head -n1 || true)"
if [[ -n "${NEWEST}" && "${NEWEST}" != "${TARBALL}" ]]; then
  warn "A newer tegraflash tarball exists but wasn't selected: ${NEWEST}"
  warn "  (selected instead: ${TARBALL})"
  warn "If you meant to unpack the newer one, re-run with IMAGE set to match it."
fi
# FLASH_DIR is env-overridable; canonicalize and refuse dangerous targets before
# the recursive delete so a typo/bad override can't wipe an important directory.
FLASH_DIR="$(realpath -m -- "${FLASH_DIR}")"
case "${FLASH_DIR}" in
  ""|/|"${REPO_ROOT}"|"${WORKROOT}"|"${BUILD_DIR}"|"${LAYERS_DIR}"|"${HOME}")
    die "refusing to remove unsafe FLASH_DIR: '${FLASH_DIR}'" ;;
esac
rm -rf -- "${FLASH_DIR}"
mkdir -p "${FLASH_DIR}"
log "Extracting into ${FLASH_DIR} ..."
tar -C "${FLASH_DIR}" -xf "${TARBALL}"

# initrd-flash bug (NVIDIA L4T R35.6.4): write_to_device() checks
# `[ -e external-secureflash.xml ]` (file exists) instead of `-s` (file
# has content) before feeding it to nvflashxmlparse --rewrite-contents-
# from. With zerosbk/no signing keys (our setup - no -u/-v keyfile),
# that file legitimately ends up empty, and nvflashxmlparse then dies
# on it with "no element found: line 1, column 0" instead of just
# skipping an empty file. Patch it post-extract since this file comes
# from NVIDIA's prebuilt tarball, not something we build ourselves.
if [[ -f "${FLASH_DIR}/initrd-flash" ]]; then
  sed -i 's/if \[ -e external-secureflash\.xml \]; then/if [ -s external-secureflash.xml ]; then/' \
    "${FLASH_DIR}/initrd-flash"
fi

log "Unpacked. Contents:"
ls -1 "${FLASH_DIR}" | sed 's/^/    /'
log "Next: put the board in recovery mode, then run scripts/05-flash-nvme.sh"
