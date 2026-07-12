# 07 — Boat Computer as a Jetson Container Host (design)

This document captures the **converged Phase‑2 design** for the boat computer.
It supersedes the "bake every app into the rootfs" sketch in
[`05-phase2-boat-computer-layer.md`](05-phase2-boat-computer-layer.md): instead
of shipping Signal K, GNSS and CAN tooling *in* the image, the Yocto image
becomes a **minimal, reliable Jetson container host**, and every application
runs as a **Docker container you pull and orchestrate with your own
`docker-compose.yml` files**.

Nothing in this file has been turned into recipes yet — it is the blueprint the
`meta-boat` scaffolding is built from. Treat package/recipe names as
**kirkstone targets to verify** (`bitbake-layers show-recipes '*name*'`); L4T
R35 / meta-tegra names drift between point releases.

## Why this shape

- **Reproducible apps without rebuilding the OS.** Signal K already ships as a
  multi-arch (arm64) image from
  [`KEGustafsson/signalk-server-dockers`](https://github.com/KEGustafsson/signalk-server-dockers)
  → `signalk/signalk-server` / GHCR. DeepStream ships from NVIDIA's NGC
  registry. Pulling images decouples app updates from Yocto image builds.
- **The host stays small and serviceable.** A container host has a tiny,
  well-understood surface: kernel + drivers + Docker + networking + time +
  thermal + display. Easier to make power-fail-safe and A/B updatable.
- **You own the composition.** You write and version the compose files on the
  data partition; the OS just runs them.

### Division of responsibility

```
        ┌──────────────────────── Jetson Xavier NX ────────────────────────┐
        │  Yocto rootfs (meta-boat)  =  CONTAINER HOST                       │
        │  ───────────────────────────────────────────────────────────────  │
        │  kernel + Tegra GPU driver   Docker + nvidia-container-runtime     │
        │  nvargus-daemon (CSI cams)   NetworkManager + ModemManager         │
        │  dbus / avahi / bluez        chrony + fake-hwclock                 │
        │  Weston compositor (GPU)     nvpmodel / jetson-clocks / nvfancontrol│
        │  openssh / nftables          watchdog + RAUC (A/B)                 │
        └───────────────┬───────────────────────────────────────────────────┘
                        │  docker compose (files on /data)
        ┌───────────────┴───────────────────────────────────────────────────┐
        │  CONTAINERS (you pull / compose)                                    │
        │   • signalk-server  (your GHCR / Docker Hub, arm64)                 │
        │   • deepstream-l4t  (nvcr.io, GPU + DLA + NVENC/NVDEC + ISP)        │
        │   • firefox         (helm UI → Weston Wayland socket)              │
        │   • influxdb / grafana / node-red  (optional dashboards)            │
        └────────────────────────────────────────────────────────────────────┘
```

Rule of thumb: **anything that owns real hardware — the GPU driver, the display
compositor, the network interfaces, the system clock, the watchdog — is on the
host. Everything else is a container.**

## What changed from the earlier scaffold

| Removed / demoted | Reason |
|-------------------|--------|
| `packagegroup-boat-canbus` (`can-utils`), `boat-can-setup`, CAN kernel modules | **NMEA 2000 / CAN is provided by an external interface**, not this box |
| `packagegroup-boat-nav` (`gpsd` + clients) | **GNSS is provided by the external interface**; the host no longer reads a GPS directly |
| `packagegroup-boat-runtime` (`nodejs`, `npm`, native build tools) | Signal K and its native plugins **build inside the container**; the host needs no Node toolchain |
| Native OpenCPN / native Signal K install | Delivered as containers |

`chrony` stays (see [Time](#time-without-a-gps-or-rtc)), now disciplined from the
**external interface / network** rather than a local GPS.

## Host package list

Grouped as `packagegroup-boat-*` sub-groups so an image can select just what it
needs. ★ = new vs the current `meta-boat`.

| Sub-group | Packages (kirkstone targets — verify names) |
|-----------|---------------------------------------------|
| ★ `-containers` | `docker-ce` (or `docker-moby`), `docker-compose`, `containerd-opencontainers`, `runc-opencontainers`, `ca-certificates` |
| ★ `-nvidia-container` ⚠️ | `nvidia-container-toolkit`, `nvidia-container-runtime`, `libnvidia-container` — **prototype first, see risks** |
| ★ `-nvidia-host` | Tegra userspace driver libs (`tegra-libraries-*`, pulled by the BSP), `nvargus-daemon` (CSI cameras), CUDA/TensorRT **not** required on host if using self-contained DeepStream containers |
| ★ `-jetson` | `nvpmodel`, `jetson-clocks`, `nvfancontrol`, `tegrastats`, **`jetson-stats` (jtop)** |
| `-connectivity` (keep+) | `networkmanager`, ★`modemmanager`, `avahi-daemon`+`avahi-utils`, ★`bluez5`, `hostapd`, `dnsmasq`, `iw`, `wireless-regdb-static`, ★`wireguard-tools`, `chrony` |
| ★ `-hmi` | `weston`, `weston-init`, `wayland`, `wayland-protocols`, `libinput`, ★`wvkbd` (on-screen keyboard), fonts (`ttf-dejavu`, `fontconfig`) — **Firefox itself is a container, not a package** |
| `-reliability` (keep+) | `watchdog`, `watchdog-keepalive`, ★`fake-hwclock`, ★`rauc` (A/B), overlay/data-partition wiring |
| ★ `-security` | `openssh` (hardened: keys-only), `nftables`, `fail2ban` |
| ★ `-nettools` | `iproute2` (`ip`/`ss`), `net-tools` (`ifconfig`/`netstat`), `iputils` (`ping`/`arping`), `bmon`, `tcpdump`, `mtr`, `traceroute`, `ethtool`, `bind-utils` (`dig`/`nslookup`), `iftop`, `curl`, `wavemon` (Wi-Fi signal), `nmap`; cellular debug: `libqmi`/`libmbim` (`qmicli`/`mbimcli`), `mmcli` (via `modemmanager`) |
| `-tools` (keep+) | `nvme-cli`, `i2c-tools`, `usbutils`, `pciutils`, `htop`, `tmux`, `rsync`, `nano`, `minicom`, ★`git` (host-side config/compose management — separate from the git inside the container), ★`iperf3` |

> `iproute2` used to live in the (now-dropped) `-canbus` group; it moves to
> `-nettools`. `git` on the host is for versioning your compose files / config
> under `/data`; the Signal K container already bundles its own `git` for
> in-container plugin builds.

### Layers, DISTRO_FEATURES, licensing

- **New layers:** `meta-virtualization` (Docker; pulls in meta-oe / meta-python /
  meta-networking / meta-filesystems). meta-tegra is already present and
  provides the Tegra driver, `nvargus-daemon` and the container runtime bits.
  `meta-tegra-community` may be needed for `jetson-stats` / helpers.
- **`DISTRO_FEATURES:append = " systemd virtualization wayland opengl"`**
- **`LICENSE_FLAGS_ACCEPTED += "commercial"`** (Tegra driver / any NVIDIA
  components) — already set in this project.

### Kernel configuration (a `.bbappend` fragment)

The meta-tegra kernel must gain, or Docker + local-device passthrough won't work:

- **Containers:** namespaces, `CGROUPS` (+`CGROUP_BPF`), `OVERLAY_FS`, `BRIDGE`,
  `VETH`, netfilter / `NF_NAT` / `IP_NF_*`. Without these `dockerd` fails to
  start.
- **Local device passthrough into containers:** `I2C_CHARDEV` (`/dev/i2c-*`),
  `SPI_SPIDEV` (`/dev/spidev*`), USB‑serial (`ftdi_sio`, `cp210x`, `ch341`,
  `pl2303`, `cdc_acm`) for any container that talks to on-board sensors.
- The **GPU driver is already in the meta-tegra kernel** — no fragment needed
  for CUDA/DeepStream at the kernel level.

## GPU / accelerators inside containers (the critical part)

DeepStream and the camera pipeline run **in a container**, but on Jetson the
accelerator stack is split — the container is *not* self-sufficient:

| In the **container** (`nvcr.io/nvidia/deepstream-l4t:6.3-*` for JP5.1/R35) | On the **host** (meta-tegra) |
|---|---|
| CUDA, cuDNN, TensorRT, DeepStream SDK, gstreamer `nvinfer`, your models/app | Tegra **kernel GPU driver** (in the kernel) |
| | Matching **Tegra userspace driver libs** (bind-mounted into the container) |
| | **nvidia-container-toolkit / -runtime** exposing GPU + **DLA** + NVENC/NVDEC + VIC + ISP |
| | **`nvargus-daemon`** for CSI cameras |

Practical wiring:

- Set the NVIDIA runtime as **default** so every compose gets the GPU:
  `/etc/docker/daemon.json` → `{"default-runtime": "nvidia", "data-root": "/data/docker"}`.
- **CSI cameras:** run `nvargus-daemon` on the host and mount its socket into the
  container: `-v /tmp/argus_socket:/tmp/argus_socket`.
- **USB cameras:** just pass `--device /dev/video0`.
- **All HW accelerators** ("light up the DLA too") is a matter of the DeepStream
  config (`enable-dla=1`, `use-dla-core=0/1`) and TensorRT builder flags inside
  the container — not extra host packages. Xavier NX has **2 DLA cores**.

## Docker host setup

- **`data-root` on the NVMe data partition** (`/data/docker`), never the small
  rootfs — DeepStream images are multiple GB.
- **`ca-certificates`** on the host so registry TLS validates.
- **Working DNS / egress** via NetworkManager; the clock must be correct before
  the first pull (see Time).
- **NGC login** for gated images: `docker login nvcr.io` with an NGC API key.
- **Compose as config-as-code (git):** the compose files live in a **git
  checkout on `/data`** (e.g. `/data/compose`) that you `git pull` on the boat to
  update settings, then `docker compose up -d`. This is why `git` is on the host
  (`-tools`). Requirements: `ca-certificates` + a correct clock for HTTPS
  (a wrong clock fails the TLS handshake, same as registry pulls); for a private
  repo use the `openssh` client with a **read-only deploy key** (or a scoped
  token) stored on `/data`, never baked into the image.
- You can keep this hand-managed *and* later bake the stable compose files into
  `meta-boat` as a systemd unit for reproducibility. Start hand-managed / git.

### Example: Signal K container

The Signal K image expects the **host** to provide dbus/avahi/bluez, time, and
device access (it detects a mounted host D-Bus socket and then skips its own
avahi — see the `startup.sh` in `signalk-server-dockers`):

```yaml
services:
  signalk:
    image: ghcr.io/kegustafsson/signalk-server:latest-ubuntu   # arm64
    network_mode: host                      # mDNS + reachability
    volumes:
      - /data/signalk:/home/node/.signalk    # persistent config on NVMe
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket  # use host avahi/bluez
    devices:
      - /dev/ttyUSB0                          # any local NMEA0183 / sensor
      - /dev/i2c-1
    group_add: ["dialout", "i2c", "spi"]
    restart: unless-stopped
```

### Example: DeepStream container (GPU + CSI camera)

```yaml
services:
  vision-ai:
    image: nvcr.io/nvidia/deepstream-l4t:6.3-samples   # JP5.1 / R35
    runtime: nvidia                # or rely on default-runtime
    network_mode: host
    volumes:
      - /tmp/argus_socket:/tmp/argus_socket             # CSI camera (Argus)
      - /data/models:/models
    devices:
      - /dev/video0                                     # USB camera (optional)
    restart: unless-stopped
```

### Example: Firefox as the helm UI

Firefox is **not** built in Yocto. Weston runs natively; the browser is a
container that connects to Weston's Wayland socket and points at the localhost
container UIs (Signal K :3000, Grafana, DeepStream web/RTSP). GPU acceleration
of the browser is optional — software rendering (llvmpipe) is fine for the
Signal K web UI; wire in the Tegra EGL libs only if a page needs it.

```yaml
services:
  firefox:
    image: <a wayland-capable firefox image, arm64>
    network_mode: host
    environment:
      - WAYLAND_DISPLAY=wayland-1
      - XDG_RUNTIME_DIR=/run/user/1000
      - MOZ_ENABLE_WAYLAND=1
    volumes:
      - /run/user/1000/wayland-1:/run/user/1000/wayland-1   # Weston socket
    devices:
      - /dev/dri                                            # GPU render node
    restart: unless-stopped
```

Boot flow for the display: systemd autologin → **Weston** starts (GPU via Tegra
EGL) → the Firefox container comes up fullscreen showing Signal K. Add a
touchscreen with `libinput`; add `wvkbd` for an on-screen keyboard on
touch-only helms.

## Time without a GPS or RTC

- **`chrony` is the NTP client/server** — do **not** also add `ntpd` or
  `systemd-timesyncd`. It syncs from the **external interface / network NTP** now
  that local gpsd is gone, and `makestep` jumps a large offset at boot.
- **Only the host runs chrony.** Containers share the host kernel clock, so one
  daemon corrects the whole system. No NTP client inside any container.
- **No RTC on the devkit:** add **`fake-hwclock`** so the clock restores to
  last-known-good instead of 1970 (a wrong clock breaks the first registry pull's
  TLS). Better still, fit a **hardware RTC (DS3231 on I²C)** and add
  `kernel-module-rtc-ds1307`.

## Networking

**NetworkManager + ModemManager** (not systemd-networkd) — a boat juggles Wi‑Fi
client (marina), Wi‑Fi AP (helm hotspot via `hostapd`/`dnsmasq`), Ethernet, and
cellular, with priority/failover. Add `wireguard-tools` for a VPN home, `avahi`
for `*.local` discovery (shared into containers), and `nftables` as the firewall.

## Preconfigured SSH

Ship the SSH server locked-down from first boot — no manual setup on the boat.
`IMAGE_FEATURES += "ssh-server-openssh"` is already set; add to `meta-boat`:

- **`authorized_keys` baked in** — your SSH **public** key(s) to
  `/home/<user>/.ssh/authorized_keys` (700/600) via a small `boat-ssh-keys.bb`
  or `ROOTFS_POSTPROCESS_COMMAND`. Key login works immediately.
- **Hardened `sshd_config` drop-in** (`/etc/ssh/sshd_config.d/10-boat.conf`) via
  an `openssh_%.bbappend`: `PasswordAuthentication no`,
  `PermitRootLogin prohibit-password`, `KbdInteractiveAuthentication no`.
- **A non-root login user** via `EXTRA_USERS_PARAMS` (`extrausers`).
- **`nftables`** rule allowing port 22.

**Do NOT bake SSH *host* keys** (or any private key / password) into the image —
a shared host key across every flashed device is a host-key-collision / MITM
risk. Let host keys generate on first boot (openssh's `sshd-keygen` service does
this automatically). Bake only *public* authorized keys and config.

## Updates

Two tiers:

- **Dev:** `IMAGE_FEATURES += "package-management"` with an ipk/opkg feed
  (`PACKAGE_FEED_URIS`) for fast iteration on the host. Not atomic — don't rely
  on it in the field.
- **Field/production:** **RAUC** (or Mender) with **A/B rootfs slots** for atomic,
  power-fail-safe, rollback-capable OS updates — the correct model for an
  unattended boat. Pair with **BUP** (meta-tegra `generate_bup_payload.sh`) for
  firmware. Apps update independently by pulling new container tags.

## Reliability

- `watchdog` tied to the Tegra hardware watchdog.
- **Read-only root + `overlayfs`** (or a dedicated writable **`/data` partition**)
  so an abrupt power cut can't corrupt the rootfs. Container volumes and Docker's
  `data-root` live on `/data`.
- Periodic `fstrim` on the NVMe; size-cap persistent journald.

## Open risks — prototype these first

1. **`nvidia-container-toolkit` on meta-tegra kirkstone.** This is the linchpin
   for GPU-in-container and the least certain piece. Validate a
   `deepstream-l4t` container seeing the GPU **before** committing to the rest.
2. **DLA / ISP exposure into the container** (accelerator visibility, not just
   the GPU).
3. **CSI camera via `nvargus-daemon` socket** across the container boundary.
4. **Firefox-in-container Wayland handshake** with Weston (socket perms,
   `XDG_RUNTIME_DIR`, GPU vs software rendering).
5. **Image size / partition growth** — Docker `data-root` on NVMe, larger APP
   partition than the current 2 GB headroom.

## Next steps (scaffolding to follow)

1. Trim `meta-boat`: drop CAN/nav/native-runtime groups; add `-containers`,
   `-nvidia-container`, `-nvidia-host`, `-jetson`, `-hmi`, `-security`.
2. Kernel `.bbappend` with the Docker + i2c/spi/usb-serial fragment.
3. `docker-daemon` config recipe (`daemon.json`: nvidia default-runtime,
   data-root on `/data`).
4. Weston autostart + autologin unit; touch input.
5. Example compose files under `meta-boat/recipes-boat/compose/` (Signal K,
   DeepStream, Firefox) + a systemd unit that `docker compose up`s them.
6. `chrony` config (external/NTP source) + `fake-hwclock`.
7. RAUC A/B integration (later hardening).
