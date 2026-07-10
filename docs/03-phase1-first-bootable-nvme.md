# 03 — Phase 1: First Bootable NVMe Image

Goal: a minimal `core-image-base` that boots the Xavier NX from the NVMe SSD.
Get this working before adding any boat software.

## TL;DR

```bash
./scripts/00-install-host-deps.sh     # once per host
./scripts/01-fetch-layers.sh          # clone poky + meta-openembedded + meta-tegra @ kirkstone
./scripts/02-configure-build.sh       # write bblayers.conf + local.conf (MACHINE + NVMe)
IMAGE=core-image-base ./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
./scripts/05-flash-nvme.sh            # board in recovery mode, SSD installed
```

All tunables live in [`scripts/env.sh`](../scripts/env.sh) and can be overridden
from the shell (e.g. `MACHINE=jetson-xavier-nx-devkit-emmc`).

## Step by step

### 1. Fetch the layers

```bash
./scripts/01-fetch-layers.sh
```

Clones into `yocto/layers/` on branch `kirkstone`:
`poky`, `meta-openembedded`, `meta-tegra`. It prints the pinned commit of each.
For a **product**, pin these to fixed tags/commits once a combination works.

### 2. Configure

```bash
./scripts/02-configure-build.sh
```

Creates `yocto/build/` and writes:

- `conf/bblayers.conf` — all layers incl. `layers/meta-boat`.
- `conf/local.conf` — with a managed block:

  ```bitbake
  MACHINE = "jetson-xavier-nx-devkit"
  LICENSE_FLAGS_ACCEPTED += "commercial"     # accept NVIDIA BSP licences
  INIT_MANAGER = "systemd"
  TNSPEC_BOOTDEV = "nvme0n1p1"                # <-- boot rootfs from NVMe
  ROOTFSPART_SIZE = "68719476736"            # 64 GiB APP partition on the SSD
  ```

  `TNSPEC_BOOTDEV = "nvme0n1p1"` is the switch that makes meta-tegra lay out the
  rootfs on the external NVMe device and emit the `initrd-flash` / `doexternal.sh`
  helpers. Set `BOOTDEV=""` in `env.sh` to fall back to the stock SD layout for a
  quick smoke test.

### 3. Build

```bash
IMAGE=core-image-base ./scripts/03-build.sh
```

Under the hood: `source poky/oe-init-build-env yocto/build && bitbake core-image-base`.
First run is long. Output lands in:

```text
yocto/build/tmp/deploy/images/jetson-xavier-nx-devkit/
    core-image-base-jetson-xavier-nx-devkit.tegraflash.tar.gz   <- flashing bundle
    core-image-base-jetson-xavier-nx-devkit.ext4                 <- rootfs
```

### 4. Unpack the flashing bundle

```bash
./scripts/04-unpack-tegraflash.sh      # -> yocto/flash/
```

Uses `tar` (never a GUI extractor — permissions/symlinks matter).

### 5. Put the board in recovery mode

1. Power off. Ensure the **NVMe SSD is installed** in the M.2 slot.
2. Connect the **USB-C OTG** port to the host.
3. Hold **FORCE RECOVERY**, tap **RESET**, release FORCE RECOVERY.
4. Verify on the host: `lsusb -d 0955:` shows an NVIDIA device.

### 6. Flash

```bash
./scripts/05-flash-nvme.sh
```

Runs `sudo ./initrd-flash`, which RCM-boots a helper Linux over USB, writes the
**firmware to QSPI** and the **rootfs to the NVMe**. First flash is slow because
of the firmware step.

- Re-flashing only the rootfs later? `./scripts/05-flash-nvme.sh --external-only`
  (skips QSPI).
- Prefer to write the SSD on your host instead of in the board?
  `./scripts/05-flash-nvme.sh --host-drive /dev/sdX` (uses `doexternal.sh`).

### 7. Boot

Disconnect USB, power-cycle. Watch the serial console (115200 8N1). You should
reach a login prompt with the rootfs on `/dev/nvme0n1p1`. Verify:

```bash
findmnt /            # source should be /dev/nvme0n1p1
lsblk
```

Once this boots reliably, move on to
[`04-flashing-nvme.md`](04-flashing-nvme.md) for flashing detail, then
[`05-phase2-boat-computer-layer.md`](05-phase2-boat-computer-layer.md).
