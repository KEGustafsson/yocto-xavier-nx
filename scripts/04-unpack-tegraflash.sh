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

# Accept the image name positionally, exactly as 03-build.sh does. Without
# this, `./scripts/03-build.sh boat-image; ./scripts/04-unpack-tegraflash.sh`
# - a completely natural sequence given 03's own usage line - resolves IMAGE to
# core-image-base and unpacks a stale Phase-1 tarball.
IMAGE="${1:-${IMAGE}}"

DEPLOY="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
# IMAGE defaults to core-image-base (env.sh) unless exported - if you built a
# different IMAGE (e.g. boat-image) and forget to export it here too, this
# silently unpacks a *different, possibly stale* tarball instead of failing
# loudly. Always echo what was actually resolved so that mismatch is visible
# before you flash it.
log "IMAGE=${IMAGE} (pass it as an argument, or export it, if this isn't what you built)"

# meta-tegra's image_types_tegra.bbclass produces <IMAGE_NAME>.tegraflash.tar.gz
# and, with TEGRAFLASH_PACKAGE_FORMAT = "zip", <IMAGE_NAME>.tegraflash.zip.
# There is no .tar.zst on kirkstone - an earlier version of this script looked
# for ".tegraflash-tar.zst", which is neither of those and matched nothing;
# dead patterns like that read as coverage that isn't there.
# The EXACT name for this IMAGE and MACHINE, with no fallback to "whatever is
# newest". There used to be one, and it is the wrong shape of helpfulness for a
# step that ends in writing firmware to a board: with IMAGE unset,
# `04-unpack-tegraflash.sh boat-image` would happily unpack a core-image-base
# tarball because that was the only one present. The name below is
# meta-tegra's own IMAGE_LINK_NAME symlink, so it exists after any successful
# build of this image - if it is missing, something is wrong and saying so is
# more useful than picking a substitute.
TARBALL="${DEPLOY}/${IMAGE}-${MACHINE}.tegraflash.tar.gz"
if [[ ! -e "${TARBALL}" ]]; then
  err "no ${IMAGE}-${MACHINE}.tegraflash.tar.gz in ${DEPLOY}"
  if compgen -G "${DEPLOY}/*.tegraflash.tar.gz" >/dev/null; then
    err "what is there:"
    ls -1t "${DEPLOY}"/*.tegraflash.tar.gz | sed 's|.*/|      |' >&2
    err "pass the image name as an argument to pick one, e.g."
    err "  ./scripts/04-unpack-tegraflash.sh boat-image"
  fi
  if compgen -G "${DEPLOY}/*.tegraflash.zip" >/dev/null; then
    err "this build produced a .tegraflash.zip; only the .tar.gz bundle is"
    err "handled here (unset TEGRAFLASH_PACKAGE_FORMAT and rebuild)."
  fi
  die "run scripts/03-build.sh, or correct IMAGE"
fi

log "Using tarball: ${TARBALL}"

# Stop if a *newer* tarball for a *different* IMAGE exists in the same deploy
# dir - the exact trap above: IMAGE resolved to something with an existing
# tarball, but it isn't the most recently built one, so you're about to flash
# stale content. This used to be a warning, which is not enough: the next step
# writes firmware to a board, the message scrolls past behind the extraction
# listing, and there is no undo.
NEWEST="$(ls -t "${DEPLOY}"/*.tegraflash.tar.gz 2>/dev/null | head -n1 || true)"
if [[ -n "${NEWEST}" && "${NEWEST}" != "${TARBALL}" ]]; then
  warn "A newer tegraflash tarball exists but wasn't selected: ${NEWEST}"
  warn "  (selected instead: ${TARBALL})"
  warn "Pass the image name as an argument to pick a different one, e.g."
  warn "  ./scripts/04-unpack-tegraflash.sh boat-image"
  confirm "Unpack the older ${TARBALL##*/} anyway?" || die "aborted"
fi
# FLASH_DIR is env-overridable; canonicalize and refuse dangerous targets before
# the recursive delete so a typo/bad override can't wipe an important directory.
FLASH_DIR="$(realpath -m -- "${FLASH_DIR}")"
WORKROOT_ABS="$(realpath -m -- "${WORKROOT}")"
case "${FLASH_DIR}" in
  ""|/|"${REPO_ROOT}"|"${WORKROOT_ABS}"|"${BUILD_DIR}"|"${LAYERS_DIR}"|"${HOME}")
    die "refusing to remove unsafe FLASH_DIR: '${FLASH_DIR}'" ;;
esac
# A blocklist only catches the mistakes someone thought of. `FLASH_DIR=.` from
# a home directory resolves to something that matches none of the names above
# and would be recursively deleted - with sudo, for anything the user cannot
# unlink. So require the target to live under WORKROOT, which is this project's
# own scratch tree, and make anything else an explicit, informed decision.
if [[ "${FLASH_DIR}" != "${WORKROOT_ABS}"/* ]]; then
  warn "FLASH_DIR is outside this project's work tree (${WORKROOT_ABS}):"
  warn "    ${FLASH_DIR}"
  warn "It is about to be DELETED RECURSIVELY and replaced with the unpacked"
  warn "flashing bundle."
  confirm "Really delete and overwrite ${FLASH_DIR}?" || die "aborted"
fi
# Two-step removal. initrd-flash runs under sudo (scripts/05-flash-nvme.sh)
# and leaves root-owned directories behind in here - bootloader_staging/,
# signed/, __pycache__/, pyfdt/__pycache__/, device-logs-*/ - all mode 0755
# root:root. An unprivileged rm cannot unlink their contents, so it exits
# non-zero and, under `set -e`, aborts the script *after* it has already
# deleted everything user-owned: a half-wiped FLASH_DIR with no extraction
# into it. Try as this user first and only escalate if something survived;
# the safety case above has already validated the path, so the sudo rm is
# bounded to a directory we know is safe to destroy.
rm -rf -- "${FLASH_DIR}" 2>/dev/null || true
if [[ -e "${FLASH_DIR}" ]]; then
  warn "root-owned leftovers from a previous sudo flash in ${FLASH_DIR}"
  warn "  removing them with sudo (you may be prompted for your password)"
  sudo rm -rf -- "${FLASH_DIR}"
fi
mkdir -p "${FLASH_DIR}"
log "Extracting into ${FLASH_DIR} ..."
tar -C "${FLASH_DIR}" -xf "${TARBALL}"

# Stamp the unpack only once tar has returned successfully, and have
# 05-flash-nvme.sh require the stamp. tar extracts in archive order, so an
# interrupted or ENOSPC extraction very plausibly leaves `initrd-flash` present
# and the payload blobs missing - and "the script exists" was the only thing
# the flash step used to check before running it against a board.
: > "${FLASH_DIR}/.boat-unpack-complete"

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
