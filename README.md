# yocto-xavier-nx

Build a Yocto (OpenEmbedded) embedded Linux for the **NVIDIA Jetson Xavier NX
Developer Kit** that **boots from an NVMe SSD — no SD card** — then grow it into
a **boat / marine embedded computer**.

Two phases:

1. **Phase 1** — a minimal image booting from NVMe. Prove the toolchain, flash
   and boot path first.
2. **Phase 2** — add the [`meta-boat`](layers/meta-boat) layer: a Jetson
   **container host** (Docker + an XFCE desktop + Jetson tooling); Signal K,
   DeepStream and a browser HMI run as containers you compose yourself, not
   baked-in packages. NMEA 2000/CAN comes from an external interface on the
   network, not from this host.

## Quick start

On an **x86-64 Linux host** (native — not a VM/WSL), with the devkit's NVMe SSD
installed:

```bash
# Phase 1 — first bootable NVMe image
./scripts/00-install-host-deps.sh     # host packages (once)
./scripts/01-fetch-layers.sh          # poky, meta-openembedded, meta-tegra,
                                      # meta-virtualization, meta-tegra-community @ kirkstone
./scripts/02-configure-build.sh       # write bblayers.conf + local.conf (MACHINE + NVMe)
IMAGE=core-image-base ./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
# put board in recovery mode (hold FORCE RECOVERY, tap RESET), USB-C to host:
./scripts/05-flash-nvme.sh
# power-cycle -> boots from /dev/nvme0n1p1

# Phase 2 — the boat computer
export IMAGE=boat-image   # keep it exported for unpack + flash too, not just build:
                          # IMAGE defaults to core-image-base, and 04 would then pick
                          # up the stale Phase-1 tarball. (03 and 04 also take the
                          # image name as an argument, and 04 now stops and asks
                          # rather than quietly unpacking an older tarball.)
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
             plus lint.sh - fast local checks, also what CI runs, and
             wake-boat.sh - send a Wake-on-LAN magic packet to the boat
wol/         host-side sleep/wake for the running boat computer: boat-sleep.sh
             and boat-wake.sh, which add the "did it actually go down / come
             back?" half that wake-boat.sh cannot give you (wol/README.md)
config/      reference local.conf / bblayers.conf
layers/
  meta-boat/ the Phase-2 container-host layer (image, packagegroup, Docker/XFCE config)
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
| `ROOTFS_SIZE_BYTES` | `17179869184` | APP partition size (16 GiB). Small on purpose — the flasher writes it non-sparse over USB 2.0. `boat-grow-rootfs --grow` claims the rest of the SSD on first boot (that command ships with `boat-image` only — a Phase 1 `core-image-base` flash does not have it) |
| `IMAGE` | `core-image-base` | build target; `boat-image` for Phase 2 |
| `YOCTO_BRANCH` | `kirkstone` | layer branch |

## Checks

`./scripts/lint.sh` runs the fast repository checks — shellcheck over every
shell script in the repo (the host scripts, `wol/`, and everything shipped to
the boat), YAML syntax, relative links in the docs, and that every recipe's
`file://` reference actually exists. Seconds, no layers fetched, no bitbake.
`.github/workflows/lint.yml` runs exactly this script on every push, so CI
and your machine can't drift apart.

It is **not** a build gate, and deliberately so: it cannot catch recipe
parse errors, unresolvable `RDEPENDS`, or packaging QA failures. That last
class needs a real image build — `bitbake -n` passes clean on it. Build
before you trust a change.

## Documentation

1. [Overview & architecture](docs/01-overview-and-architecture.md)
2. [Host prerequisites](docs/02-host-prerequisites.md)
3. [Phase 1 — first bootable NVMe image](docs/03-phase1-first-bootable-nvme.md)
4. [Flashing to NVMe in detail](docs/04-flashing-nvme.md)
5. [Phase 2 — the boat computer layer](docs/05-phase2-boat-computer-layer.md)
6. [Troubleshooting](docs/06-troubleshooting.md)

Plus [`wol/README.md`](wol/README.md) for putting the boat computer to sleep
and waking it again from your laptop.

## Requirements

- x86-64 Linux host (Ubuntu 20.04/22.04 best-tested), native, ~150 GB free,
  16 GB+ RAM. Newer hosts (24.04+, 26.04) work too: `scripts/00-install-host-deps.sh`
  installs `gcc-12 g++-12` for kirkstone's `-native` recipes, and
  `scripts/02-configure-build.sh` points those builds at them when they are
  present — see [host prerequisites](docs/02-host-prerequisites.md).
- Xavier NX devkit, NVMe M.2 SSD, USB-C cable, barrel-jack PSU, USB-TTL serial.

## Status & caveats

- Phase 1 (NVMe boot, `core-image-base`) has been built, flashed, and verified
  booting on real Xavier NX hardware with rootfs on `/dev/nvme0n1p1`. Phase 2
  (`boat-image`/`meta-boat`) is now the **Jetson container-host** design in
  [`docs/05-phase2-boat-computer-layer.md`](docs/05-phase2-boat-computer-layer.md)
  (Docker + an XFCE desktop + Jetson tooling on the host;
  Signal K/DeepStream/Firefox run as containers) rather than the earlier
  "bake everything into the rootfs" scaffold. It has been built, flashed and
  booted, XFCE desktop included: the recipes carry the specific findings from
  those boots, marked `CONFIRMED ON HARDWARE` — the jtop group, the i2c/spi
  gid pinning, the greyed-out Shut Down buttons that polkit fixed, and the
  ~110s every boot that masking `systemd-networkd-wait-online` removed.
  `scripts/01-fetch-layers.sh` fetches `meta-virtualization` and
  `meta-tegra-community` for Phase 2, and `scripts/02-configure-build.sh`
  additionally enables the `meta-xfce`, `meta-gnome` and `meta-multimedia`
  sublayers of the `meta-openembedded` clone for the desktop.
- Remote power control (`boat-power`: Wake-on-LAN + `boat-sleep` for SC7 deep
  sleep, see [Phase 2](docs/05-phase2-boat-computer-layer.md#power-wake-on-lan-and-remote-sc7-suspend))
  has been exercised on hardware — a full sleep and magic-packet wake, with
  the boot and suspend/resume timings in `boat-wol.service` and
  `boat-wol-dispatcher.sh` measured on this board. `boat-sleep` refuses to
  suspend when nothing can wake the board again, so a board whose PHY cannot
  do it fails safe rather than needing a visit; `boat-sleep --status` answers
  that question on the bench in a second.
- **Credentials, stated plainly.** `boat-image` is a bench/development image:
  root and the `boat` user both have an **empty password**, sshd is built with
  `PermitRootLogin yes` and `PermitEmptyPasswords yes`, and `boat` has
  passwordless `sudo` and is in the `docker` group (which is root-equivalent).
  Anyone who can reach port 22 has root with no credential. This is a
  deliberate choice, declared in `boat-image.bb` rather than inherited from
  `debug-tweaks` by accident — reverse it before the boat goes anywhere near
  an untrusted network. See docs/05 "Build-time user & SSH".
- Pin layer commits (`scripts/01-fetch-layers.sh` prints them) before treating
  a build as a product; `kirkstone` branches move.
- Package names in `packagegroup-boat` target kirkstone; if one is missing on
  your snapshot, `bitbake-layers show-recipes '*name*'` and adjust.
- Not affiliated with NVIDIA or the OE4T project. NVIDIA BSP components are used
  under their respective licences (`LICENSE_FLAGS_ACCEPTED += "commercial"`).
