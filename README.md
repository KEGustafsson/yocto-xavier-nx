# yocto-xavier-nx

Build a Yocto (OpenEmbedded) embedded Linux for the **NVIDIA Jetson Xavier NX
Developer Kit** that **boots from an NVMe SSD — no SD card** — then grow it into
a **boat / marine embedded computer**.

Two phases:

1. **Phase 1** — a minimal image booting from NVMe. Prove the toolchain, flash
   and boot path first.
2. **Phase 2** — add the [`meta-boat`](layers/meta-boat) layer: a Jetson
   **container host** (Docker + Weston + Jetson tooling); Signal K, GNSS/CAN
   bridging, DeepStream and a browser HMI run as containers you compose
   yourself, not baked-in packages.

## Quick start

On an **x86-64 Linux host** (native — not a VM/WSL), with the devkit's NVMe SSD
installed:

```bash
# Phase 1 — first bootable NVMe image
./scripts/00-install-host-deps.sh     # host packages (once)
./scripts/01-fetch-layers.sh          # poky + meta-openembedded + meta-tegra @ kirkstone
./scripts/02-configure-build.sh       # write bblayers.conf + local.conf (MACHINE + NVMe)
IMAGE=core-image-base ./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
# put board in recovery mode (hold FORCE RECOVERY, tap RESET), USB-C to host:
./scripts/05-flash-nvme.sh
# power-cycle -> boots from /dev/nvme0n1p1

# Phase 2 — the boat computer
export IMAGE=boat-image   # must stay exported for unpack + flash too, not just build -
                           # IMAGE defaults to core-image-base otherwise and these
                           # scripts will silently unpack/flash the WRONG (stale) image
./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
./scripts/05-flash-nvme.sh --skip-bootloader
```

Prefer a single-command reproducible build? Use kas:
`kas build kas/xavier-nx-nvme.yml`.

## How it works (short version)

The Xavier NX **module holds the boot firmware in QSPI-NOR flash**, separate
from OS storage. We flash **firmware → QSPI** (once, over USB in recovery mode)
and write the **rootfs → NVMe**. UEFI then boots Linux from the SSD. The switch
that puts the rootfs on NVMe is one line in `local.conf`:

```
TNSPEC_BOOTDEV = "nvme0n1p1"
```

meta-tegra emits an `initrd-flash` helper in the build's `.tegraflash.tar.gz`
that writes both. Full explanation in
[`docs/01-overview-and-architecture.md`](docs/01-overview-and-architecture.md).

> **Branch:** everything is on **`kirkstone`** (L4T R35.6.4 / JetPack 5.1.6).
> meta-tegra `master` has dropped standalone Xavier NX (Orin/Thor only), so
> kirkstone is the correct branch for this board.

## Repository layout

```
scripts/     deps → fetch → configure → build → unpack → flash  (all read scripts/env.sh)
config/      reference local.conf / bblayers.conf
layers/
  meta-boat/ the Phase-2 container-host layer (image, packagegroup, Docker/Weston config)
kas/         optional kas-based reproducible build
docs/        the full guide (start at 01)
```

Everything cloned/built lands under `yocto/` (git-ignored), keeping this
checkout clean.

## Configuration

All knobs are in [`scripts/env.sh`](scripts/env.sh) and override from the shell:

| Variable | Default | Meaning |
|----------|---------|---------|
| `MACHINE` | `jetson-xavier-nx-devkit` | or `jetson-xavier-nx-devkit-emmc` |
| `BOOTDEV` | `nvme0n1p1` | rootfs device; empty = stock SD layout |
| `ROOTFS_SIZE_BYTES` | `68719476736` | APP partition size (64 GiB) |
| `IMAGE` | `core-image-base` | build target; `boat-image` for Phase 2 |
| `YOCTO_BRANCH` | `kirkstone` | layer branch |

## Documentation

1. [Overview & architecture](docs/01-overview-and-architecture.md)
2. [Host prerequisites](docs/02-host-prerequisites.md)
3. [Phase 1 — first bootable NVMe image](docs/03-phase1-first-bootable-nvme.md)
4. [Flashing to NVMe in detail](docs/04-flashing-nvme.md)
5. [Phase 2 — the boat computer layer](docs/05-phase2-boat-computer-layer.md)
6. [Troubleshooting](docs/06-troubleshooting.md)

## Requirements

- x86-64 Linux host (Ubuntu 20.04/22.04 best-tested), native, ~150 GB free,
  16 GB+ RAM. Newer hosts (24.04+, 26.04) work too but need `gcc-12 g++-12`
  installed manually first - see [host prerequisites](docs/02-host-prerequisites.md).
- Xavier NX devkit, NVMe M.2 SSD, USB-C cable, barrel-jack PSU, USB-TTL serial.

## Status & caveats

- Phase 1 (NVMe boot, `core-image-base`) has been built, flashed, and verified
  booting on real Xavier NX hardware with rootfs on `/dev/nvme0n1p1`. Phase 2
  (`boat-image`/`meta-boat`) is now the **Jetson container-host** design in
  [`docs/05-phase2-boat-computer-layer.md`](docs/05-phase2-boat-computer-layer.md)
  (Docker + Weston + Jetson tooling on the host; Signal K/DeepStream/Firefox
  run as containers) rather than the earlier "bake everything into the
  rootfs" scaffold. It has been built successfully (0 errors) but not yet
  flashed or booted on hardware, so treat it as unverified until that
  happens. `scripts/01-fetch-layers.sh` now also fetches
  `meta-virtualization` and `meta-tegra-community` for it.
- Pin layer commits (`scripts/01-fetch-layers.sh` prints them) before treating
  a build as a product; `kirkstone` branches move.
- Package names in `packagegroup-boat` target kirkstone; if one is missing on
  your snapshot, `bitbake-layers show-recipes '*name*'` and adjust.
- Not affiliated with NVIDIA or the OE4T project. NVIDIA BSP components are used
  under their respective licences (`LICENSE_FLAGS_ACCEPTED += "commercial"`).
