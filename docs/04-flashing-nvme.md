# 04 — Flashing to NVMe in Detail

This chapter explains what the flashing scripts do and the options you have.

## Recovery mode, every time

Flashing always requires the board in **recovery mode** with the USB-C OTG port
connected to an **x86-64 Linux host running natively**:

1. Power off (use the DC barrel jack, not USB, for power).
2. Hold **FORCE RECOVERY**, tap **RESET**, release.
3. Confirm: `lsusb -d 0955:` lists an NVIDIA Corp. device.

If a soft `reboot forced-recovery` misbehaves, power-cycle into recovery from
cold instead — the firmware sometimes doesn't set up USB correctly after a soft
reboot.

## Path A — `initrd-flash` (SSD installed in the Jetson) — recommended

```bash
cd yocto/flash
sudo ./initrd-flash                 # firmware (QSPI) + rootfs (NVMe)
sudo ./initrd-flash --external-only # only the rootfs (fast; firmware already done)
sudo ./initrd-flash --qspi-only     # only the firmware
```

`scripts/05-flash-nvme.sh` wraps this and passes through extra flags:

```bash
./scripts/05-flash-nvme.sh                 # full flash
./scripts/05-flash-nvme.sh --external-only # rootfs only on subsequent flashes
```

How it works: the script RCM-boots a minimal Linux onto the board over USB; that
on-board Linux sees the NVMe drive and writes the partitions to it, and writes
boot firmware to the module's QSPI. Progress is high-level; on failure read the
`log.initrd-flash.<timestamp>` file it names.

## Path B — `doexternal.sh` (write the SSD on your host)

Useful for provisioning many drives, or when the in-board NVMe write is flaky.

```bash
# 1. Flash firmware to the module at least once (Path A, --qspi-only is fine).
# 2. Attach the target SSD to the HOST (USB-NVMe enclosure or M.2 slot).
lsblk                                        # identify the drive, e.g. /dev/sdb
./scripts/05-flash-nvme.sh --host-drive /dev/sdb
# 3. Move the SSD into the Jetson's M.2 slot and boot.
```

> **DANGER:** double-check the `/dev/sdX` name. Writing the wrong device destroys
> that disk. The script shows `lsblk` and asks for confirmation; there is no undo.

## What ends up on the SSD

A GPT with a small ESP (UEFI) and the **APP** partition (`nvme0n1p1`) holding the
ext4 rootfs, kernel, device tree and `extlinux.conf`. `ROOTFSPART_SIZE` in
`local.conf` controls the APP partition size (default 64 GiB). It must be ≥ your
image and ≤ the SSD capacity.

## Signing / secure boot (later)

meta-tegra supports UEFI Secure Boot and bootloader signing. Set
`TEGRA_UEFI_DB_KEY` / `TEGRA_UEFI_DB_CERT` (build-time signing) or pass
`-u/-v/--user_key` to `initrd-flash` for post-build signing. Out of scope for a
first bring-up; revisit for production.

## Common flashing pitfalls

- **VM / WSL / ARM host** → USB timeouts. Use a native x86-64 Linux host.
- **TLP installed** → USB power cuts mid-flash. `sudo apt remove tlp` + reboot.
- **Cheap USB cable/hub** → transient `0955:` disconnects. Use a good cable,
  a direct port, power-cycle, retry.
- **`cp: cannot stat 'signed/*'`** → partition-layout/size mismatch; check
  `ROOTFSPART_SIZE` vs SSD size.

Next: [`05-phase2-boat-computer-layer.md`](05-phase2-boat-computer-layer.md)
