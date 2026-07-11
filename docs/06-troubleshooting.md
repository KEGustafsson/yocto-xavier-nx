# 06 — Troubleshooting

## Build

| Symptom | Fix |
|---------|-----|
| `Please use a locale setting which supports UTF-8` | `sudo locale-gen en_US.UTF-8`; ensure `LANG=en_US.UTF-8`. |
| `Do not use Bitbake as root` | Build as a normal user. The scripts enforce this. |
| `Nothing PROVIDES 'X'` for a boat package | Recipe name differs on your branch. `bitbake-layers show-recipes '*X*'` and adjust `packagegroup-boat.bb`. |
| `LICENSE_FLAGS_ACCEPTED` errors on NVIDIA components | Keep `LICENSE_FLAGS_ACCEPTED += "commercial"` in `local.conf`. |
| Out of disk during build | `scripts/02-configure-build.sh` already enables `rm_work`; if you're still tight, free more disk or move `WORKROOT` to a bigger volume. |
| Layer parse/compat error | All layers must be on the **same** `kirkstone` branch. Re-run `scripts/01-fetch-layers.sh`. |
| `-native` recipe fails to compile (gnulib/`__has_builtin`/K&R errors, GCC-version-looking messages) | Host GCC is too new for kirkstone. Install `gcc-12 g++-12` (`scripts/02-configure-build.sh` points `-native` builds at them automatically once installed) - see `02-host-prerequisites.md`. |
| `AttributeError: module 'ast' has no attribute 'Str'` (usually during `do_rootfs`/`do_populate_lic`) | Python 3.12+ removed `ast.Str`; bitbake's own `oe.license` still uses it. `scripts/pyfix/sitecustomize.py` shims this automatically via `PYTHONPATH` - make sure `scripts/03-build.sh` is what you're using to invoke bitbake (not a bare `bitbake` in a differently-set-up shell). |
| `_pickle.PicklingError` from bitbake's hash-equivalence server on startup | Python 3.14 changed `multiprocessing`'s default start method; also handled by `scripts/pyfix/sitecustomize.py`. Same fix as above. |

## Flashing

| Symptom | Fix |
|---------|-----|
| `lsusb -d 0955:` shows nothing | Board isn't in recovery mode. Cold power-cycle: hold FORCE RECOVERY, tap RESET, release. |
| USB timeouts / drops mid-flash | Use a **native x86-64 Linux host** (no VM/WSL), a good USB-C cable, a direct port; remove **TLP**. |
| `cp: cannot stat 'signed/*'` during Step 1 | Usually harmless (unused branch of NVIDIA's `odmsign.func`) - the flash normally continues past it. Only a real problem if Step 4 then fails; see below. |
| Step 4 fails: `xml.etree.ElementTree.ParseError: no element found` / `ERR: write failure to external storage` | Real `initrd-flash` bug: `write_to_device()` checks `[ -e external-secureflash.xml ]` instead of `-s`, and with zerosbk signing (no `-u`/`-v` keyfile) that file is legitimately empty. `scripts/04-unpack-tegraflash.sh` patches this automatically - re-run it (after `sudo rm -rf yocto/flash` if a previous `sudo` flash left root-owned files behind) rather than reusing an unpatched unpack. |
| `initrd-flash` fails late for another reason | Read the named `log.initrd-flash.<timestamp>`; add `--debug` for verbose logs. |
| Permissions errors | Run the flash under `sudo` (the scripts already do). |
| Wrong `/dev/sdX` for `--host-drive` | Re-check `lsblk`; writing the wrong disk is irreversible. |
| Rootfs write seems to hang | Not actually hung, just slow: 64 GiB over recovery-mode USB 2.0 realistically takes 20-30 minutes. Only worry if it's stalled well past that. |

## Boot

| Symptom | Fix |
|---------|-----|
| No serial output at all | Check UART wiring (115200 8N1), power on barrel jack + jumper, that firmware flashed. |
| UEFI shows but no OS found | Rootfs not written to NVMe, or SSD not seated. Re-run rootfs flash; confirm the SSD enumerates in the UEFI device list. |
| Boots but `mount \| grep ' / '` isn't `/dev/nvme0n1p1` (`findmnt` isn't installed on `core-image-base`) | `TNSPEC_BOOTDEV` wasn't set at build time. Rebuild with `TNSPEC_BOOTDEV = "nvme0n1p1"`, reflash. |
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
