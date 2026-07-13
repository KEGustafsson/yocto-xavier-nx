#!/usr/bin/env bash
# Flash the Xavier NX. Two modes:
#
#   (default) initrd-flash  - board in recovery mode, NVMe SSD installed in the
#                             Jetson's M.2 slot, USB-C OTG cable to this host.
#                             Writes boot firmware to QSPI + rootfs to the NVMe.
#
#   --host-drive /dev/sdX   - write the external rootfs to an SSD attached
#                             DIRECTLY to this host (via doexternal.sh), then
#                             move the SSD into the Jetson. Firmware must have
#                             been flashed at least once via initrd-flash first.
#
# Extra flags after the mode are passed through to the underlying script, e.g.
#   scripts/05-flash-nvme.sh --skip-bootloader     # skip the slow QSPI firmware step
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

[[ -d "${FLASH_DIR}" ]] || die "nothing unpacked; run scripts/04-unpack-tegraflash.sh first"
cd "${FLASH_DIR}"

if [[ "${1:-}" == "--host-drive" ]]; then
  DEV="${2:-}"
  [[ -b "${DEV}" ]] || die "usage: $0 --host-drive /dev/sdX   (block device not found)"
  [[ -x ./doexternal.sh ]] || die "doexternal.sh not found - was the image built with TNSPEC_BOOTDEV set to an NVMe device?"
  warn "This will ERASE ${DEV}:"
  lsblk -o NAME,SIZE,MODEL "${DEV}" || true
  confirm "Overwrite ${DEV}?" || die "aborted"
  log "Writing external rootfs to ${DEV} via doexternal.sh ..."
  sudo ./doexternal.sh "${DEV}"
  log "Done. Install this drive in the Jetson's NVMe slot and boot."
  exit 0
fi

# --- initrd-flash path (SSD installed in the Jetson) ----------------------
[[ -x ./initrd-flash ]] || die "initrd-flash not found in ${FLASH_DIR}"

log "Checking for a Jetson in recovery mode (USB 0955:xxxx) ..."
if command -v lsusb >/dev/null 2>&1; then
  if lsusb -d 0955: >/dev/null 2>&1 && lsusb -d 0955: | grep -q .; then
    log "  found: $(lsusb -d 0955:)"
  else
    warn "No NVIDIA device (0955:) detected on USB."
    warn "Put the board in recovery mode: hold FORCE RECOVERY, tap RESET, release;"
    warn "connect the USB-C OTG port to this host, then re-run."
    confirm "Continue anyway?" || die "aborted"
  fi
fi

warn "Ensure the NVMe SSD is installed in the Jetson's M.2 slot before flashing."
log "Running: sudo ./initrd-flash ${*}"
sudo ./initrd-flash "$@"

log "Flash complete. Disconnect USB, power-cycle the board; it should boot from ${BOOTDEV}."
log "If it fails to boot, see docs/06-troubleshooting.md (e.g. blank-SD-card workaround)."
