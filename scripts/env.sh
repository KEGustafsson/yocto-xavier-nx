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
# Must be >= your image size, and must leave 1 GiB of headroom below the SSD's
# capacity: scripts/02-configure-build.sh derives
# TEGRA_EXTERNAL_DEVICE_SECTORS from this value PLUS 1 GiB, for the kernel,
# recovery, esp and UDA partitions that share the drive. Setting it equal to
# the SSD size therefore describes a layout larger than the disk.
#
# 16 GiB, deliberately much smaller than the SSD you will actually fit.
# make-sdcard writes the APP partition with a plain, NON-sparse dd, so every
# byte of this size crosses the recovery-mode USB 2.0 link whether it holds
# data or not - and boat-image's real content is around 5 GiB. At 64 GiB
# (the previous default) that meant 20-30 minutes per flash, almost all of
# it spent transferring zeros; at 16 GiB it is roughly a quarter of that.
#
# The rest of the drive is not lost, just not claimed at flash time: run
# `boat-grow-rootfs --grow` once on the booted board and it extends the APP
# partition and the ext4 over the whole SSD in seconds. See
# docs/05-phase2-boat-computer-layer.md "Reclaiming the rest of the SSD".
#
# Raise it if you would rather not run that step (e.g. an unattended factory
# flash), or if the image grows past ~12 GiB. Lower it and the build fails in
# do_image_ext4 rather than producing something that won't boot.
: "${ROOTFS_SIZE_BYTES:=17179869184}"

# --- A/B OS updates (EXPERIMENTAL) -----------------------------------------
# 1 = two rootfs slots (APP + APP_b, each ROOTFS_SIZE_BYTES), GRUB on the ESP
# picking the slot with automatic fallback, and SWAP + DATA partitions -
# docs/plans/ab-os-updates.md. Off by default until the plan's Phase 7
# validation has passed on hardware. Changing it changes the partition
# layout: the first flash after switching it on (or off) is a full
# 05-flash-nvme.sh, and it ERASES the SSD. Needs BOOTDEV on NVMe.
: "${AB_LAYOUT:=0}"
# The release string an A/B image writes to /etc/boat-release and its update
# bundle carries; RAUC and the OTA check compare these as (relaxed) semantic
# versions, so a later release must compare higher - e.g. 1.4.0, 2026.10.1.
# Empty = "0.0.0-dev", fine for bench work but never offered over the air
# (every dev build has that same version). Set it for anything you ship.
# This and the two below reach bitbake only through
# scripts/02-configure-build.sh (it writes them into local.conf): after a
# change, run 02 again - 03-build.sh stops on a value local.conf lacks.
: "${BOAT_RELEASE:=}"
# RAUC's min-bundle-version (plan D6): the oldest release an A/B image will
# install, on every path - over the air, scripts/push-update.sh, a USB
# stick. Empty = no floor. Raise it to a release that fixed a security
# problem, so that no board running it can be put back below it
# (docs/07-updates.md). Never above BOAT_RELEASE.
: "${BOAT_MIN_BUNDLE_VERSION:=}"
# Where the update signing keys live (scripts/gen-update-keys.sh). Outside
# the repository AND outside WORKROOT on purpose: yocto/ gets deleted, and
# losing the CA means reflashing every board over USB.
: "${BOAT_KEYS_DIR:=${HOME}/.local/share/boat-update-keys}"
# Bench images only (docs/plans/ab-validation.md): BOAT_BENCH=1 makes an A/B
# image also trust the bench CA (scripts/gen-update-keys.sh --bench), which
# signs the fault-injection bundles; BOAT_FAULT picks the fault
# boat-bundle-faulty carries (panic, emergency, tier2, hang). Never set
# BOAT_BENCH for an image that goes on a boat.
: "${BOAT_BENCH:=0}"
: "${BOAT_BENCH_KEYS_DIR:=${HOME}/.local/share/boat-update-bench-keys}"
: "${BOAT_FAULT:=}"

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

export YOCTO_BRANCH MACHINE BOOTDEV ROOTFS_SIZE_BYTES AB_LAYOUT BOAT_RELEASE BOAT_MIN_BUNDLE_VERSION BOAT_KEYS_DIR \
  BOAT_BENCH BOAT_BENCH_KEYS_DIR BOAT_FAULT IMAGE
export REPO_ROOT WORKROOT LAYERS_DIR BUILD_DIR FLASH_DIR
