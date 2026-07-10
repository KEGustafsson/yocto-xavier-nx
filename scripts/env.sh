# shellcheck shell=bash
# ---------------------------------------------------------------------------
# Central configuration for the Xavier NX / NVMe Yocto build.
# Every script sources this file. Override any value from your shell, e.g.:
#     MACHINE=jetson-xavier-nx-devkit-emmc ./scripts/03-build.sh
# ---------------------------------------------------------------------------

# --- Yocto / meta-tegra release ------------------------------------------
# Xavier NX support lives on the "kirkstone" branch of every layer.
# (meta-tegra 'master' has dropped standalone Xavier NX and is Orin/Thor only.)
# kirkstone currently tracks L4T R35.6.4 / JetPack 5.1.6.
: "${YOCTO_BRANCH:=kirkstone}"

# --- Target machine -------------------------------------------------------
# jetson-xavier-nx-devkit        -> SD-slot devkit module (P3668-0000)
# jetson-xavier-nx-devkit-emmc   -> eMMC module (P3668-0001) in devkit carrier
# Either module boots the rootfs from NVMe once TNSPEC_BOOTDEV is set below;
# the module's QSPI-NOR always holds the boot firmware.
: "${MACHINE:=jetson-xavier-nx-devkit}"

# --- Boot device ----------------------------------------------------------
# Root filesystem (APP partition) target. "nvme0n1p1" = first NVMe SSD.
# Leave empty (BOOTDEV="") to keep the stock SD-card layout.
# Note: '=' (not ':=') so an explicit empty value is respected, not re-defaulted.
: "${BOOTDEV=nvme0n1p1}"

# Size in bytes of the rootfs partition created on the NVMe drive.
# 64 GiB by default; must be <= the SSD size and >= your image size.
: "${ROOTFS_SIZE_BYTES:=68719476736}"

# --- Image to build -------------------------------------------------------
# Phase 1 (first bootable): core-image-base
# Phase 2 (boat computer):  boat-image   (provided by meta-boat)
: "${IMAGE:=core-image-base}"

# --- Layout on disk -------------------------------------------------------
# Everything lives under WORKROOT so the checkout stays clean.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${WORKROOT:=${REPO_ROOT}/yocto}"     # holds layers/ and build/
: "${LAYERS_DIR:=${WORKROOT}/layers}"
: "${BUILD_DIR:=${WORKROOT}/build}"
: "${FLASH_DIR:=${WORKROOT}/flash}"     # unpacked tegraflash tarball

export YOCTO_BRANCH MACHINE BOOTDEV ROOTFS_SIZE_BYTES IMAGE
export REPO_ROOT WORKROOT LAYERS_DIR BUILD_DIR FLASH_DIR
