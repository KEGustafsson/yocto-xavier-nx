# 01 — Overview & Architecture

## Goal

Build an embedded Linux system with the Yocto Project for the **NVIDIA Jetson
Xavier NX Developer Kit** that boots entirely from an **NVMe SSD** — no SD card
in normal operation. Then extend it into a **boat / marine embedded computer**.

The work is split into two phases:

1. **Phase 1 — first bootable NVMe image.** A minimal console image
   (`core-image-base`) that boots from the SSD. Prove the toolchain, flashing
   and boot path end-to-end before adding complexity.
2. **Phase 2 — the boat computer.** Add the `meta-boat` layer: GNSS, NMEA 2000
   (CAN), MQTT, Wi-Fi access point, a Node.js runtime for Signal K, watchdog,
   and service tooling.

## How the Xavier NX boots

The Xavier NX **module** carries a **QSPI-NOR flash** that holds the boot
firmware chain (BootROM → MB1 → MB2 → TegraBoot/cboot → **UEFI**). This is
separate from the OS storage. That is the key to NVMe boot:

```
  ┌─────────────── Xavier NX module ───────────────┐
  │  QSPI-NOR flash:  BootROM → MB1/MB2 → UEFI      │
  └───────────────────────┬─────────────────────────┘
                          │ UEFI boot order
                          ▼
        ┌──────────────── NVMe M.2 SSD ─────────────┐
        │  GPT: (esp) + APP partition = Linux rootfs │
        │        kernel + DTB + extlinux + rootfs    │
        └────────────────────────────────────────────┘
```

- We flash the **firmware** into QSPI once (via USB while the board is in
  *recovery mode*).
- We write the **root filesystem** (the "APP" partition) to the **NVMe SSD**.
- On boot, UEFI finds the kernel/extlinux on the NVMe and mounts the rootfs
  from there. No SD card is needed for the OS.

> Some early L4T releases required a *blank* SD card to be present in the slot
> even when booting from NVMe. On the L4T R35 UEFI firmware this is normally
> not needed; keep it as a fallback (see `06-troubleshooting.md`).

## The software stack (layers)

| Layer | Provides | Branch |
|-------|----------|--------|
| **poky** (OE-Core + bitbake) | build system, base recipes | `kirkstone` |
| **meta-openembedded** | gpsd, can-utils, mosquitto, nodejs, chrony … | `kirkstone` |
| **meta-tegra** (OE4T) | Jetson BSP: kernel, UEFI, `tegraflash`, `initrd-flash` | `kirkstone` |
| **meta-boat** (this repo) | marine software + image | — |

### Why the `kirkstone` branch?

meta-tegra's `master` branch has moved to **L4T R39 / JetPack 7** and now
supports only Orin and Thor — **standalone Xavier NX has been dropped there**.
Xavier NX lives on **`kirkstone`**, which tracks **L4T R35.6.4 / JetPack 5.1.6**.
All four layers must be on the *same* release branch (`kirkstone`).

### Machine names

| MACHINE | Module |
|---------|--------|
| `jetson-xavier-nx-devkit` | SD-slot devkit module **P3668-0000** (default here) |
| `jetson-xavier-nx-devkit-emmc` | eMMC module **P3668-0001** |

Both boot the OS from NVMe once `TNSPEC_BOOTDEV = "nvme0n1p1"` is set; the
difference is only where the *firmware* lives (both use the module QSPI).

## Flashing model (meta-tegra "initrd flashing")

meta-tegra builds a **`.tegraflash.tar.gz`** artifact alongside the image. Unpack
it and you get flashing helpers:

- **`initrd-flash`** — RCM-boots a small Linux over USB onto the board (in
  recovery mode), which then writes the QSPI firmware **and** the rootfs to the
  NVMe SSD *installed in the Jetson*. This is the primary path for external boot.
- **`doexternal.sh /dev/sdX`** — alternatively writes the external rootfs to an
  SSD attached **directly to your host**; you then move the SSD into the Jetson.
  (Firmware still needs `initrd-flash` at least once.)

The host that runs these **must be x86-64 Linux, running natively** (not a VM) —
NVIDIA's low-level tools are x86-64 binaries and USB-sensitive.

## Repository map

```
scripts/       automation: deps → fetch → configure → build → unpack → flash
config/        reference local.conf / bblayers.conf
layers/meta-boat/   the Phase-2 marine layer
kas/           optional kas-based reproducible build
docs/          this guide
```

Next: [`02-host-prerequisites.md`](02-host-prerequisites.md)
