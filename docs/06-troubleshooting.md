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

## bitbake itself

| Symptom | Fix |
|---------|-----|
| `ERROR: Command '...buildTargets...' failed: Busy (buildTargets in progress)`, after a long series of `Retrying server connection` | Not a build failure. bitbake keeps a memory-resident server alive between runs; if a previous client was interrupted (Ctrl-C at the wrong moment, closed terminal, killed pid) the server survives and holds the build dir. `scripts/03-build.sh` now clears this automatically before building; to do it by hand, `bitbake -m` (`--kill-server`) from a configured shell. Cancel bitbake with `bitbake -m` rather than killing the client, and you won't hit it. |
| A build you started elsewhere gets killed when you run `scripts/03-build.sh` | That is the cleanup above doing its job — only one bitbake can own a build directory. Re-run with `BOAT_KEEP_BITBAKE_SERVER=1` to leave the running server alone and get the `Busy` error instead. |

## Boot

| Symptom | Fix |
|---------|-----|
| No serial output at all | Check UART wiring (115200 8N1), power on barrel jack + jumper, that firmware flashed. |
| UEFI shows but no OS found | Rootfs not written to NVMe, or SSD not seated. Re-run rootfs flash; confirm the SSD enumerates in the UEFI device list. |
| Boots but `mount \| grep ' / '` isn't `/dev/nvme0n1p1` (`findmnt` isn't installed on `core-image-base`) | `TNSPEC_BOOTDEV` wasn't set at build time. Rebuild with `TNSPEC_BOOTDEV = "nvme0n1p1"`, reflash. |
| **Won't boot from NVMe on older firmware** | Insert a **blank** SD card (no ESP/APP partitions) as a fallback, or update the module firmware (`--qspi-only` flash) to the R35 UEFI build. |

## Helm display / XFCE desktop (Phase 2)

The desktop is Xorg + XFCE started from the `boat` user's tty1 autologin -
see [`05-phase2-boat-computer-layer.md`](05-phase2-boat-computer-layer.md)
"HMI / XFCE autostart". Two logs cover almost everything:
`~boat/.local/share/xorg/Xorg.0.log` (the X server) and
`/tmp/boat-xfce-session.log` (`startx` + the session). SSH in rather than
debugging on the console - the console *is* the thing that's broken.

| Symptom | Fix |
|---------|-----|
| Console autologins, screen stays black, nothing in `/tmp/boat-xfce-session.log` | The `/etc/profile.d` guard didn't fire. It requires UID 2000 (`BOAT_HMI_UID`), `$DISPLAY` unset and `tty` = `/dev/tty1`; check `id -u`, and that the login shell is bash (`profile.d` is not read by `sh`). |
| `xf86OpenConsole: Cannot open virtual console` / `Cannot open /dev/dri/card0` in `Xorg.0.log` | The unprivileged Xorg has no logind session to get devices from. `loginctl` should list one session for `boat`, `seat0`, `tty1`; if not, `pam` is missing from `DISTRO_FEATURES` or `pam_systemd` isn't in `/etc/pam.d/login`. |
| Xorg starts but fails loading the NVIDIA driver | Confirm `xserver-xorg-video-nvidia` and `tegra-configs-xorg` are installed (`/usr/lib/xorg/modules/drivers/nvidia_drv.so`, `/etc/X11/xorg.conf`) and that `boat` is in the `video` group - meta-tegra's udev rules give `/dev/nvhost-*` to that group. As a bisect, run `startx /usr/bin/boat-xfce-session -- :0 vt1` as root from tty1 (`su -`; `sudo` is not in this image): if root works and `boat` doesn't, it's a permissions/logind problem, not a driver one. |
| Desktop comes up but the keyboard layout is wrong | `/etc/X11/xorg.conf.d/10-boat-keyboard.conf` (`XkbLayout`, from `BOAT_HMI_XKB_LAYOUT`, default `fi`). Read at X server start, so restart the session after changing it - or `setxkbmap <layout>` for a live test. |
| Screen blanks after ~10 minutes | X's built-in screensaver; `xfce4-power-manager` isn't installed. `xset s off -dpms`, or add `-s 0 -dpms` to the `startx` server args in `boat-xfce-autostart.sh` to make it permanent. |
| Container GUI app: `cannot open display :0` / `Authorization required` | The container needs `DISPLAY=:0`, `/tmp/.X11-unix` bind-mounted, **and** the session cookie: `XAUTHORITY=/run/boat-x11/Xauthority` with `/run/boat-x11:/run/boat-x11:ro` mounted (see docs/05 "Container GUI apps on the HDMI screen (X11)"). Check the export exists and is current: `ls -l /run/boat-x11/Xauthority`, then `XAUTHORITY=/run/boat-x11/Xauthority DISPLAY=:0 xdpyinfo \| head -3` from the desktop session. If it is missing, `/tmp/boat-xfce-session.log` says why. Mount the **directory**, not the file - the session rewrites the file on every restart, and a file mount would leave the container on the old inode. |
| Screen flickers between console and black, repeatedly | The session is crash-looping: `xfce4-session` (or Xorg) exits, the login shell ends, agetty autologins again. `/tmp/boat-xfce-session.log` names the failure; a missing package from `packagegroup-xfce-base` is the usual cause. |

## Sleep / Wake-on-LAN (Phase 2)

`boat-sleep --status` first, always - it reports the sleep state the kernel
offers and whether an interface is armed, without changing anything. See
[`05-phase2-boat-computer-layer.md`](05-phase2-boat-computer-layer.md)
"Power: Wake-on-LAN and remote SC7 suspend".

| Symptom | Fix |
|---------|-----|
| `boat-sleep` refuses: *no interface is armed for magic-packet wake* | Working as designed - it won't strand the board. `boat-wol-arm` prints the reason per interface: no such interface (wrong `BOAT_WOL_INTERFACES` in `/etc/default/boat-power`), driver reports no `g` in `Supports Wake-on`, or the flag didn't stick. `--force` suspends anyway, with no way back but a power cycle. |
| `boat-sleep` refuses: *no 'deep' state offered* | `cat /sys/power/mem_sleep` shows only `s2idle`, so this kernel/BSP isn't giving you SC7. Check `CONFIG_SUSPEND`/`CONFIG_PM_SLEEP` really landed (`zcat /proc/config.gz \| grep -E 'CONFIG_(SUSPEND\|PM_SLEEP)='`) and that `boat-power.cfg` is in the kernel recipe's `SRC_URI`. |
| `boat-wol-arm`: *NIC has the magic-packet flag, but its wake source is 'disabled'* | Both halves of the wake path have to be up, so this counts as not armed. The device's `power/wakeup` attribute wouldn't take `enabled` - check `cat /sys/class/net/eth0/device/power/wakeup` and whether anything (a udev rule, a `power/control` setting) is turning it back off. Until it does, a magic packet reaches the NIC but the board sleeps through it. |
| `systemctl status boat-wol` is red at every boot | The interface can't be armed at all - same `boat-wol-arm` messages, in `journalctl -u boat-wol`. On a driver with no magic-packet support, no amount of config fixes it; the wake path has to be something else (a physical button, or a relay on the power feed). |
| Board suspends, magic packet does nothing | Nothing acknowledges a magic packet, so work backwards. (1) Was it armed *before* the sleep? `journalctl -b -1 -u boat-wol` and the system-sleep hook's `pre` line. (2) Same layer-2 segment? `255.255.255.255` is never routed - from another subnet you need a directed broadcast *and* a router that forwards it; from ashore, use the WireGuard tunnel. (3) `tcpdump -i eth0 -n 'udp port 9'` from a third machine on the segment shows whether the packet is even on the wire. |
| Board wakes by itself, or reboots a minute into every sleep | If it *reboots*, suspect the hardware watchdog still counting across SC7: set `BOAT_SLEEP_STOP_WATCHDOG=1` in `/etc/default/boat-power`. If it *wakes*, another device is a wake source - `grep enabled /sys/devices/*/power/wakeup` and disable the ones you don't want, or find the culprit in `journalctl -b` right after the resume. |
| Resumes, but nothing on the network can reach it | The link came back without its address, or WoL wasn't re-armed. `journalctl -b \| grep boat-power` covers the hook's `post` run; `nmcli device status` covers the link. Containers keep running through a suspend but any connection they held (MQTT, a registry pull) drops and has to reconnect. |
| `boat-sleep` exits non-zero with a logind message about inhibitors | Something holds an inhibitor lock (`systemd-inhibit --list` names it - the XFCE session is the usual one). `boat-sleep --force` goes past them. |
| `boat-sleep`: *must run as root* | It writes `/sys`. There is no `sudo` in this image - `ssh root@<boat> boat-sleep`. |

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
