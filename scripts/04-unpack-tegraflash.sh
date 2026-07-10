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
rm -rf "${FLASH_DIR}"
mkdir -p "${FLASH_DIR}"
log "Extracting into ${FLASH_DIR} ..."
tar -C "${FLASH_DIR}" -xf "${TARBALL}"

log "Unpacked. Contents:"
ls -1 "${FLASH_DIR}" | sed 's/^/    /'
log "Next: put the board in recovery mode, then run scripts/05-flash-nvme.sh"
