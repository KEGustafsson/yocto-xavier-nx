# 06 — Troubleshooting

## Build

| Symptom | Fix |
|---------|-----|
| `Please use a locale setting which supports UTF-8` | `sudo locale-gen en_US.UTF-8`; ensure `LANG=en_US.UTF-8`. |
| `Do not use Bitbake as root` | Build as a normal user. The scripts enforce this. |
| `Nothing PROVIDES 'X'` for a boat package | Recipe name differs on your branch. `bitbake-layers show-recipes '*X*'` and adjust `packagegroup-boat.bb`. |
| `LICENSE_FLAGS_ACCEPTED` errors on NVIDIA components | Keep `LICENSE_FLAGS_ACCEPTED += "commercial"` in `local.conf`. |
| Out of disk during build | Yocto tmp is large; free ~150 GB. `INHERIT += "rm_work"` reduces `tmp/work` size. |
| Layer parse/compat error | All layers must be on the **same** `kirkstone` branch. Re-run `scripts/01-fetch-layers.sh`. |

## Flashing

| Symptom | Fix |
|---------|-----|
| `lsusb -d 0955:` shows nothing | Board isn't in recovery mode. Cold power-cycle: hold FORCE RECOVERY, tap RESET, release. |
| USB timeouts / drops mid-flash | Use a **native x86-64 Linux host** (no VM/WSL), a good USB-C cable, a direct port; remove **TLP**. |
| `cp: cannot stat 'signed/*'` | Partition-table/size mismatch — check `ROOTFSPART_SIZE` vs the actual SSD size. |
| `initrd-flash` fails late | Read the named `log.initrd-flash.<timestamp>`; add `--debug` for verbose logs. |
| Permissions errors | Run the flash under `sudo` (the scripts already do). |
| Wrong `/dev/sdX` for `--host-drive` | Re-check `lsblk`; writing the wrong disk is irreversible. |

## Boot

| Symptom | Fix |
|---------|-----|
| No serial output at all | Check UART wiring (115200 8N1), power on barrel jack + jumper, that firmware flashed. |
| UEFI shows but no OS found | Rootfs not written to NVMe, or SSD not seated. Re-run rootfs flash; confirm the SSD enumerates in the UEFI device list. |
| Boots but `findmnt /` isn't `/dev/nvme0n1p1` | `TNSPEC_BOOTDEV` wasn't set at build time. Rebuild with `TNSPEC_BOOTDEV = "nvme0n1p1"`, reflash. |
| **Won't boot from NVMe on older firmware** | Insert a **blank** SD card (no ESP/APP partitions) as a fallback, or update the module firmware (`--qspi-only` flash) to the R35 UEFI build. |

## CAN / NMEA 2000 (Phase 2)

| Symptom | Fix |
|---------|-----|
| `can0` missing | CAN kernel modules absent. Ensure `boat-image` pulls `kernel-module-mttcan`/`kernel-module-can*`; check `dmesg \| grep -i can`. |
| `candump` silent | No transceiver / termination / bus power, or wrong bitrate. NMEA 2000 = 250 kbit/s; verify `/etc/default/boat-can0`. |
| Interface won't come up | `ip -details link show can0`; check `boat-can0.service` status/logs. |

## Where to get help

- meta-tegra docs: <https://oe4t.github.io/> (pick the `kirkstone` book)
- OE4T discussions: <https://github.com/OE4T/meta-tegra/discussions>
- Yocto manuals: <https://docs.yoctoproject.org/>
- Signal K: <https://signalk.org/> · canboat: <https://github.com/canboat/canboat>
