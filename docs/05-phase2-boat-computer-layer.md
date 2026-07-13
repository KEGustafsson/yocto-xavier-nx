# 05 — Phase 2: The Boat Computer Layer (`meta-boat`)

**Status: `boat-image` has been built, flashed, and booted on real Xavier NX
hardware.** Confirmed working: `boat-hmi-autostart` (autologin + Weston on
tty1, giving a working Wayland session/terminal), `dockerd` (`docker ps`
responds), and networking (internet reachable). Not yet confirmed: GPU access
in containers via `nvidia-container-toolkit` — still the biggest open risk,
see "Open risks" and "What's built vs deferred" at the end.

`meta-boat` turns the plain NVMe-booting image from Phase 1 into a **minimal,
reliable Jetson container host**, not a box with Signal K/GNSS/CAN tooling
baked into the rootfs. Every application — Signal K, DeepStream, a browser
HMI — runs as a **Docker container** you pull and orchestrate with your own
`docker-compose.yml` files on the target. This is a deliberate design change
from an earlier "bake everything into the image" sketch; see "What changed"
below if you're wondering why `gpsd`/`can-utils`/`nodejs` aren't host
packages anymore.

## Build it

With a plain image booting from NVMe, add the marine software via the
`meta-boat` layer (already in `bblayers.conf`). Build the product image
instead of `core-image-base`:

```bash
export IMAGE=boat-image   # keep exported for unpack + flash, not just build - see the
                           # warning 04-unpack-tegraflash.sh now prints if you forget
./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
./scripts/05-flash-nvme.sh --skip-bootloader     # firmware already flashed in Phase 1
```

Nothing else changes — same NVMe boot path, same flashing flow. **Gotcha:**
`IMAGE` defaults to `core-image-base` (`scripts/env.sh`) unless exported for
*every* step - setting it only on the `03-build.sh` line and forgetting it on
`04`/`05` silently unpacks and flashes the wrong (stale Phase 1) image with
no error. `scripts/04-unpack-tegraflash.sh` now warns if a newer tarball for
a different `IMAGE` exists in the deploy dir than the one it picked.
`scripts/01-fetch-layers.sh` (and `kas/xavier-nx-nvme.yml`) additionally
fetch **meta-virtualization** (Docker; also unlocks meta-tegra's
`external/virtualization-layer` overlay providing `nvidia-container-toolkit`
and `libnvidia-container-*`) and **meta-tegra-community**
(`python3-jetson-stats`/`jtop`).

## Why this shape

- **Reproducible apps without rebuilding the OS.** Signal K already ships as
  a multi-arch (arm64) image from
  [`KEGustafsson/signalk-server-dockers`](https://github.com/KEGustafsson/signalk-server-dockers).
  DeepStream ships from NVIDIA's NGC registry. Pulling images decouples app
  updates from Yocto image builds.
- **The host stays small and serviceable.** A container host has a tiny,
  well-understood surface: kernel + drivers + Docker + networking + time +
  thermal + display. Easier to make power-fail-safe and A/B updatable.
- **You own the composition.** You write and version the compose files on
  the data partition; the OS just runs them.

### Division of responsibility

```text
        ┌──────────────────────── Jetson Xavier NX ────────────────────────┐
        │  Yocto rootfs (meta-boat)  =  CONTAINER HOST                       │
        │  ───────────────────────────────────────────────────────────────  │
        │  kernel + Tegra GPU driver   Docker + nvidia-container-runtime     │
        │  nvargus-daemon (CSI cams)   NetworkManager + ModemManager         │
        │  dbus / avahi / bluez        chrony                                │
        │  Weston compositor (GPU)     nvpmodel / jetson-clocks / nvfancontrol│
        │  openssh / nftables          watchdog                              │
        └───────────────┬───────────────────────────────────────────────────┘
                        │  docker-compose (files on /data)
        ┌───────────────┴───────────────────────────────────────────────────┐
        │  CONTAINERS (you pull / compose)                                    │
        │   • signalk-server  (your GHCR / Docker Hub, arm64)                 │
        │   • deepstream-l4t  (nvcr.io, GPU + DLA + NVENC/NVDEC + ISP)        │
        │   • firefox         (helm UI → Weston Wayland socket)              │
        │   • influxdb / grafana / node-red  (optional dashboards)            │
        └────────────────────────────────────────────────────────────────────┘
```

Rule of thumb: **anything that owns real hardware — the GPU driver, the
display compositor, the network interfaces, the system clock, the watchdog —
is on the host. Everything else is a container.**

## What changed from the earlier scaffold

| Removed / demoted | Reason |
|-------------------|--------|
| `packagegroup-boat-canbus` (`can-utils`), `boat-can-setup`, CAN kernel modules | **NMEA 2000 / CAN is provided by an external interface**, not this box |
| `packagegroup-boat-nav` (`gpsd` + clients) | **GNSS is provided by the external interface**; the host no longer reads a GPS directly |
| `packagegroup-boat-runtime` (`nodejs`, `npm`, native build tools) | Signal K and its native plugins **build inside the container**; the host needs no Node toolchain |
| `mosquitto` (host MQTT broker) | Any MQTT bridging Signal K needs runs inside its own container now |

`chrony` stays (see [Time](#time-without-a-gps-or-rtc)), now disciplined from
the **network** rather than a local GPS.

## What you get

`boat-image` = `core-image-base` + `packagegroup-boat` + the local
`boat-docker-config`/`boat-hmi-autostart`/`boat-compose` recipes. Grouped so
you can trim it — package names below were cross-checked against this
project's actual fetched layers (kirkstone), not guessed:

| Sub-group | Packages |
|-----------|----------|
| `-containers` | `docker-ce` (meta-virtualization's default `virtual/docker` provider — `docker-moby` is a valid alternative but gets skipped as a runtime target unless you override the preference), `python3-docker-compose`, `ca-certificates` |
| `-nvidia-container` ⚠️ | `nvidia-container-toolkit` (pulls in `libnvidia-container-tools` + `tegra-configs-container-csv`) — **unproven on kirkstone, prototype first, see risks** |
| `-nvidia-host` | `tegra-argus-daemon` (CSI cameras). Tegra userspace driver libs (`tegra-libraries-*`) are already pulled in by the BSP, not listed again |
| `-jetson` | `tegra-nvpmodel`, `tegra-nvfancontrol`, `tegra-tools` (`jetson_clocks`/`tegrastats`), `python3-jetson-stats` (jtop) |
| `-connectivity` | `networkmanager`, `modemmanager`, `avahi-daemon`+`avahi-utils`, `bluez5`, `hostapd`, `dnsmasq`, `iw`, `wireless-regdb-static`, `wireguard-tools`, `chrony` |
| `-hmi` | `weston`+`weston-init`, `wayland`, `wayland-protocols`, `libinput`, `ttf-dejavu-sans`, `fontconfig` — Firefox itself is a container, not a package |
| `-reliability` | `watchdog` (not `watchdog-keepalive` too — upstream declares them mutually exclusive alternatives) |
| `-security` | `openssh`, `nftables` |
| `-nettools` | `iproute2`, `net-tools`, `iputils`, `bmon`, `tcpdump`, `mtr`, `traceroute`, `ethtool`, `iftop`, `curl`, `nmap`, `libqmi`/`libmbim` (cellular debug) |
| `-tools` | `nvme-cli`, `i2c-tools`, `usbutils`, `pciutils`, `htop`, `tmux`, `rsync`, `nano`, `minicom`, `git` (for `/data/compose`, separate from the git inside any container), `iperf3` |

Not available in this project's fetched kirkstone-era layers, and
deliberately **omitted** rather than left as names that fail the build:
`wvkbd` (on-screen keyboard), `fail2ban`, `wavemon`, `bind-utils`
(`dig`/`nslookup`), `fake-hwclock`. Add them from a newer layer snapshot if
you need them. See
[`../layers/meta-boat/recipes-core/packagegroups/packagegroup-boat.bb`](../layers/meta-boat/recipes-core/packagegroups/packagegroup-boat.bb)
for the authoritative, commented list.

### Layers, DISTRO_FEATURES, licensing

- **New layers:** `meta-virtualization` (Docker; needs meta-oe / meta-python /
  meta-networking / meta-filesystems, already present). meta-tegra provides
  the Tegra driver, `nvargus-daemon` and the container-runtime bits once
  meta-virtualization's `virtualization-layer` collection is present.
  `meta-tegra-community` provides `jetson-stats`.
- `scripts/02-configure-build.sh` sets
  `DISTRO_FEATURES:append = " virtualization wayland opengl pam"` in
  `local.conf` (build-wide, not per-image — `DISTRO_FEATURES` gates other
  recipes' `REQUIRED_DISTRO_FEATURES` at parse time, so an image-recipe-local
  append can't retroactively unskip them). `pam` isn't just a `weston-init`
  gate: with systemd init, `pam_systemd` is what makes `systemd-logind`
  create the `/run/user/<uid>` session directory the Weston autostart (and
  any container mounting that socket) depends on.
- **`LICENSE_FLAGS_ACCEPTED += "commercial"`** (Tegra driver / NVIDIA
  components) — already set in this project.

### Kernel configuration

`layers/meta-boat/recipes-kernel/linux/linux-tegra_5.10.bbappend` merges a
`boat-docker.cfg` fragment (the standard kernel-yocto `.cfg` mechanism, same
pattern as meta-tegra's own `systemd.cfg`/`spiflash.cfg`) adding:

- **Containers:** namespaces, `CGROUPS` (+`CGROUP_BPF`), `OVERLAY_FS`,
  `BRIDGE`, `VETH`, netfilter / `NF_NAT` / `IP_NF_*`. Without these `dockerd`
  fails to start.
- **Local device passthrough into containers:** `I2C_CHARDEV`
  (`/dev/i2c-*`), `SPI_SPIDEV` (`/dev/spidev*`), USB-serial (`ftdi_sio`,
  `cp210x`, `ch341`, `pl2303`, `cdc_acm`) for any container that talks to
  on-board sensors.
- The **GPU driver is already in the meta-tegra kernel** — no fragment
  needed for CUDA/DeepStream at the kernel level.

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

- `boat-docker-config` sets the NVIDIA runtime as **default** so every
  compose gets the GPU: `/etc/docker/daemon.json` →
  `{"default-runtime": "nvidia", "data-root": "/data/docker"}`.
- **CSI cameras:** `tegra-argus-daemon` runs on the host; mount its socket
  into the container: `-v /tmp/argus_socket:/tmp/argus_socket`.
- **USB cameras:** just pass `--device /dev/video0`.
- **All HW accelerators** ("light up the DLA too"): the DeepStream config
  (`enable-dla=1`, `use-dla-core=0/1`) and TensorRT builder flags only
  *select* accelerators that the **host driver + `nvidia-container-runtime`
  already expose** into the container — they don't grant access on their
  own. So the host-side prerequisites above (driver libs, toolkit, device
  nodes, and for cameras `nvargus-daemon`) must be in place first; the flags
  then choose GPU vs DLA vs VIC/NVENC/NVDEC/ISP. Xavier NX has **2 DLA
  cores**.

## Docker host setup

- **`data-root` on the NVMe data partition** (`/data/docker`), never the
  small rootfs — DeepStream images are multiple GB. `boat-docker-config`
  points `daemon.json` there, but the `/data` partition/mount itself isn't
  provisioned by any recipe yet (see "Reliability").
- **`ca-certificates`** on the host so registry TLS validates.
- **Working DNS / egress** via NetworkManager; the clock must be correct
  before the first pull (see Time).
- **NGC login** for gated images: `docker login nvcr.io` with an NGC API
  key.
- **Compose as config-as-code (git):** compose files live in a **git
  checkout on `/data`** (e.g. `/data/compose`) that you `git pull` on the
  boat to update settings, then `docker-compose up -d`. This is why `git` is
  on the host (`-tools`). Requirements: `ca-certificates` + a correct clock
  for HTTPS (a wrong clock fails the TLS handshake, same as registry pulls);
  for a private repo use the `openssh` client with a **read-only deploy
  key** (or a scoped token) stored on `/data`, never baked into the image.

### Deploying an app: Signal K

`boat-compose` ships example compose files under
`/usr/share/boat/compose-examples/` (read-only reference) and
`boat-compose.service`, a systemd unit that runs the operator's own compose
stack from `/data/compose` if one has been seeded there:

```bash
mkdir -p /data/compose
cp /usr/share/boat/compose-examples/signalk.yml.example /data/compose/docker-compose.yml
# edit: pin the image digest, point /dev/ttyUSB0 at your GNSS/NMEA adapter
systemctl start boat-compose        # or: docker-compose -f /data/compose/docker-compose.yml up -d
```

Put `/data/compose` under git for versioned, pull-to-update config. Note the
CLI verb: this project's kirkstone-era meta-virtualization only packages the
Python-based **v1** compose client (`python3-docker-compose`), so it's
`docker-compose up -d` (hyphenated), not the `docker compose up -d` v2
plugin syntax used in some upstream docs.

The Signal K image expects the **host** to provide dbus/avahi/bluez, time,
and device access (it detects a mounted host D-Bus socket and then skips its
own avahi — see the `startup.sh` in `signalk-server-dockers`):

```yaml
services:
  signalk:
    image: ghcr.io/kegustafsson/signalk-server@sha256:<digest>   # pin, see note
    network_mode: host                      # mDNS + reachability
    volumes:
      - /data/signalk:/home/node/.signalk    # persistent config on NVMe
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket  # use host avahi/bluez
    devices:
      - /dev/ttyUSB0                          # any local NMEA0183 / sensor
      - /dev/i2c-1
    group_add: ["990", "989"]                 # NUMERIC host GIDs (i2c, spi) — see note
    restart: unless-stopped
```

- **Pin images by digest**, not mutable tags (`latest-*`, `6.3-samples`).
  Record the deployed digest in the git-tracked compose, keep the previous
  compose/image set, and roll back to it if a health check fails after an
  update.
- **`group_add` resolves group *names* inside the container image**, not on
  the host — so use **numeric host GIDs** (find them with
  `getent group i2c`). The `signalk-server-dockers` base does define
  `i2c=990`/`spi=989`/`docker=991`, so names happen to work *for that
  image*, but numeric GIDs are portable across images and unambiguous.
- **Host D-Bus mount is broad:** giving a container
  `/run/dbus/system_bus_socket` exposes *all* host D-Bus services, not just
  Avahi/BlueZ. It matches the upstream `startup.sh` contract, but for
  hardening prefer a **filtered D-Bus proxy** (e.g. `xdg-dbus-proxy`) that
  whitelists only `org.freedesktop.Avahi` and `org.bluez`.

### Deploying an app: DeepStream (GPU + CSI camera)

```yaml
services:
  vision-ai:
    image: nvcr.io/nvidia/deepstream-l4t:6.3-samples   # JP5.1 / R35
    runtime: nvidia                # or rely on Docker's default-runtime
    network_mode: host
    volumes:
      - /tmp/argus_socket:/tmp/argus_socket             # CSI camera (Argus)
      - /data/models:/models
    devices:
      - /dev/video0                                     # USB camera (optional)
    restart: unless-stopped
```

**OPEN RISK:** validate `docker run --runtime nvidia ...` sees the GPU on
this machine/L4T combo before relying on this — `nvidia-container-toolkit`
on meta-tegra kirkstone is unproven, see "Open risks" below.

### Deploying an app: Firefox as the helm UI

Firefox is **not** built in Yocto and does **not** connect to `boat-hmi-autostart`'s
Weston session via a shared Wayland socket — that native-Wayland-in-container
approach is fragile in practice (exact UID match, `XDG_RUNTIME_DIR`
permissions, waiting for the socket to exist, no widely-used prebuilt image
actually does it for Firefox) and isn't what's actually deployed here.

Instead: **[`linuxserver/firefox`](https://docs.linuxserver.io/images/docker-firefox/)**,
which has official `arm64v8` builds and runs its own browser-accessible
desktop (KasmVNC) rather than needing the host's Wayland socket at all. This
is also more useful on a boat: any phone/tablet/laptop browser on the LAN can
reach the helm UI, not just whatever's plugged into the HDMI port.

```yaml
services:
  firefox:
    image: lscr.io/linuxserver/firefox:latest
    container_name: firefox
    security_opt:
      - seccomp=unconfined   # optional, quiets some sandbox syscall warnings
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    volumes:
      - /data/firefox/config:/config   # persistent browser profile on NVMe
    ports:
      - 3000:3000   # http://<jetson-ip>:3000 - KasmVNC, any browser, any device on the LAN
    shm_size: "1gb"
    restart: unless-stopped
```

`boat-hmi-autostart`'s local Weston/tty1 session is still useful on its own
(a kiosk-style local display showing whatever's convenient, e.g. this same
KasmVNC URL pointed at `localhost:3000` in a local browser) — it's just not
wired *into* the Firefox container via Wayland socket sharing.

## HMI / Weston autostart

`boat-hmi-autostart` autologins a fixed `boat` user (UID 2000, created via
`extrausers` in `boat-image.bb`) on tty1 and launches Weston from a
`/etc/profile.d/` script on that session — **not** `weston-init`'s own
`weston.service`, which runs Weston as a separate `weston` system user, not
the UID a container's `/run/user/<uid>` mount is pinned to. Because the
login goes through a real getty + PAM session (`pam_systemd`,
`DISTRO_FEATURES` `pam`), `systemd-logind` creates `/run/user/2000/wayland-1`.

**CONFIRMED ON HARDWARE, and the reason this doesn't just call
`weston-init`'s own `weston-start` script:** `weston-start` unconditionally
launches weston through `su -c "..." $WESTON_USER`. With `$WESTON_USER`
unset (our case — we're already logged in as the right user via getty
autologin, no privilege switch needed), that becomes `su` with no target
user, which defaults to **root**, and separately `su` refuses to run at all
unless it's the foreground process group of the controlling tty. The
symptoms were exactly "su: must be run from a terminal" printed to the
console, and — once that specific failure mode was worked around — weston
silently running as **root** instead of `boat` (breaking the
`/run/user/2000` socket ownership the whole design depends on).
`boat-weston-autostart.sh` sidesteps all of this by calling `weston` (with
`--modules=systemd-notify.so`, matching what `weston-start` would have
passed) directly — no `su`, no `weston-start`.

Boot flow: systemd autologin (`boat`, UID 2000) → Weston starts (GPU via
Tegra EGL). Add a touchscreen with `libinput`; there's no on-screen-keyboard
package available on this kirkstone snapshot (`wvkbd` isn't packaged) for a
touch-only helm.

`BOAT_HMI_USER`/`BOAT_HMI_UID` in
[`boat-hmi-autostart.bb`](../layers/meta-boat/recipes-boat/hmi-autostart/boat-hmi-autostart.bb)
must keep matching whatever `extrausers` creates in `boat-image.bb` — they
default to `boat`/`2000` in both places.

## Container GUI apps on the HDMI screen (X11 via XWayland)

Not every containerized GUI app is Wayland-native or ships a KasmVNC-style
web desktop like `linuxserver/firefox`. For plain X11 apps (e.g. OpenCPN),
`boat-hmi-autostart` also launches **XWayland** so they can render straight
onto the HDMI screen via Weston, instead of needing a browser on another
device to view them.

**Packaging gotcha, confirmed on hardware:** weston's `xwayland` support is
gated on `x11` + `wayland` both being in `DISTRO_FEATURES` (it is, see
above) — but the built `xwayland.so` compositor module ships in a
**separate package**, `weston-xwayland`, not bundled into plain `weston`.
Installing just `weston` + enabling `xwayland=true` in `weston.ini` without
also pulling in `weston-xwayland` makes weston **fatally** fail to load the
module at startup ("cannot open shared object file") and crash-loop the
whole console session (autologin → crash → getty restarts → autologin →
crash..., visible as screen flicker between a login screen and black).
`packagegroup-boat-hmi` includes `weston-xwayland` for exactly this reason —
don't drop it.

Also note the **weston.ini directive changed** between weston versions:
this project's weston 10.0.2 wants `xwayland=true` in `[core]`, not the
older `modules=xwayland.so` form (weston prints a deprecation warning and
still tries to load it the old way, but the packaging gotcha above applies
either way).

Once XWayland is up, `boat-weston-autostart.sh` polls for its socket
(`/tmp/.X11-unix/X0`) and runs `xhost +local:` (no auth, local-machine-only)
so containers can connect. Compose your container with:

```yaml
services:
  x11-app:
    image: <your X11 app image, arm64>
    network_mode: host
    environment:
      - DISPLAY=:0
    volumes:
      - /tmp/.X11-unix:/tmp/.X11-unix
    restart: unless-stopped
```

See
[`x11-app.yml.example`](../layers/meta-boat/recipes-boat/compose/files/x11-app.yml.example)
for the shipped copy of this template. No `.Xauthority` mount needed — the
`xhost +local:` grant handles that instead of the cookie-based auth some
X11-in-docker examples use.

**Concrete example, CONFIRMED ON HARDWARE:**
[`signalk-kiosk.yml.example`](../layers/meta-boat/recipes-boat/compose/files/signalk-kiosk.yml.example)
combines Signal K with a normal-mode (not `--kiosk`) Chromium container
(`zenika/alpine-chrome`) pointed at `http://localhost:3000`, so Signal K's
web admin UI shows up directly on the boat's own HDMI screen, full browser
chrome (tabs, address bar) included, instead of needing a browser on
another device. Confirmed end-to-end on this Jetson: XWayland rendering,
`shm_size` (default 64MB was a black-screen cause), and an automatic-
maximize workaround (see the service's own comments for why
`--start-maximized`/`--start-fullscreen` don't work under Weston's
minimal `desktop-shell` here, and why the fix is a synthetic mouse click
at a hardcoded, screen-resolution-specific coordinate instead). The
`signalk-server` service in that file is adapted from a known-working
external stack, not this project's own `signalk.yml.example` — see the
comments at the top of the file for what changed and why.

### Build-time user & SSH (not implemented — future direction)

The `boat` user above is a **fixed scaffold**, not the interactive
build-time flow this section originally sketched: prompting the builder for
a username/password at `scripts/02-configure-build.sh` time, hashing it
(`openssl passwd -6`), and writing a gitignored `conf/site-auth.conf`
consumed by `extrausers`. That flow — plus baking `authorized_keys` and a
hardened `sshd_config` drop-in (`PasswordAuthentication no`,
`PermitRootLogin prohibit-password`) — is real, useful work but deliberately
deferred; replace the fixed `useradd -u 2000 ... boat` in `boat-image.bb`
with it when it lands. **Do not** bake SSH *host* keys (or any private
key/password) into the image — a shared host key across every flashed
device is a MITM risk; let host keys generate on first boot.

## Time without a GPS or RTC

- **`chrony` is the NTP client/server** — do **not** also add `ntpd` or
  `systemd-timesyncd`. It syncs from the **network** now that there's no
  local GPS refclock; no config change is needed from the stock recipe.
- **Only the host runs chrony.** Containers share the host kernel clock, so
  one daemon corrects the whole system. No NTP client inside any container.
- **No RTC on the devkit, and `fake-hwclock` isn't packaged** in this
  project's fetched layers — the clock reads 1970 until the first NTP sync,
  which breaks the first registry pull's TLS if there's no network yet.
  Fit a **hardware RTC (DS3231 on I²C)** and add
  `kernel-module-rtc-ds1307`, or backport the `fake-hwclock` recipe from a
  newer layer snapshot.

## Networking

**NetworkManager + ModemManager** (not systemd-networkd) — a boat juggles
Wi-Fi client (marina), Wi-Fi AP (helm hotspot via `hostapd`/`dnsmasq`),
Ethernet, and cellular, with priority/failover. `wireguard-tools` is
packaged for a VPN home; `avahi` for `*.local` discovery (shared into
containers via the D-Bus mount above); `nftables` as the firewall.

## Updates (not implemented — future direction)

Two tiers:

- **Dev:** `IMAGE_FEATURES += "package-management"` with an ipk/opkg feed
  (`PACKAGE_FEED_URIS`) for fast iteration on the host. Not atomic — don't
  rely on it in the field.
- **Field/production:** **RAUC** (or Mender) with **A/B rootfs slots** for
  atomic, power-fail-safe, rollback-capable OS updates — the correct model
  for an unattended boat. Pair with **BUP** (meta-tegra
  `generate_bup_payload.sh`) for firmware. Apps update independently by
  pulling new **digest-pinned** images (see the Signal K note): bump the
  digest in the git-tracked compose, keep the previous compose/image set,
  health-check after `up`, and roll back to the prior digest on failure.
  Avoid deploying floating `latest` tags to the boat.

RAUC needs the `meta-rauc` layer, not yet fetched by this project — a
deliberate follow-up, not started.

## Reliability

- `watchdog` tied to the Tegra hardware watchdog.
- **Read-only root + `overlayfs`** (or a dedicated writable **`/data`
  partition**) so an abrupt power cut can't corrupt the rootfs. Container
  volumes and Docker's `data-root` are meant to live on `/data` — but that
  partition isn't provisioned by any recipe here yet, so until it is,
  `boat-image`'s rootfs has extra headroom
  (`IMAGE_ROOTFS_EXTRA_SPACE = "4194304"`) as a stopgap.
- Periodic `fstrim` on the NVMe; size-cap persistent journald.

## Open risks — prototype these first

None of this has been built or flashed yet, so all of the following are
unresolved:

1. **`nvidia-container-toolkit` on meta-tegra kirkstone.** This is the
   linchpin for GPU-in-container and the least certain piece. Validate a
   `deepstream-l4t` container seeing the GPU **before** committing to the
   rest.
2. **DLA / ISP exposure into the container** (accelerator visibility, not
   just the GPU).
3. **CSI camera via `nvargus-daemon` socket** across the container boundary.
4. **Image size / partition growth** — Docker `data-root` on NVMe, and the
   `/data` partition itself doesn't exist yet.

(The Firefox-in-container Wayland-socket-handshake risk from an earlier draft
of this doc is gone — the actual deployed approach, `linuxserver/firefox`,
doesn't touch the host's Wayland socket at all. See "Deploying an app:
Firefox as the helm UI" above.)

## What's built vs deferred

- ✅ `packagegroup-boat` trimmed to the container-host design
  (`-containers`, `-nvidia-container`, `-nvidia-host`, `-jetson`, `-hmi`,
  `-security`, `-nettools`, updated `-connectivity`/`-reliability`/`-tools`).
- ✅ Kernel `.bbappend` with the Docker + i2c/spi/usb-serial fragment.
- ✅ `boat-docker-config` (`daemon.json`: nvidia default-runtime, data-root
  on `/data` — the `/data` mount itself is not provisioned). **Confirmed on
  hardware:** `dockerd` starts and `docker ps` responds.
- ✅ `boat-hmi-autostart` (fixed `boat`/UID 2000 autologin + Weston on
  tty1, direct `weston` exec - not `weston-start`, see "HMI / Weston
  autostart" for why). No touchscreen-specific calibration wired up.
  **Confirmed on hardware:** boots straight to a working Wayland
  session/terminal, no login prompt.
- ✅ XWayland for X11-only containerized GUI apps (`weston-xwayland`
  package, `xwayland=true` in `weston.ini`, `xhost +local:` grant). See
  "Container GUI apps on the HDMI screen". Recipes fixed and confirmed
  buildable; not yet flashed/booted with this specific fix.
- ✅ `boat-compose` (example compose files including `x11-app.yml.example` +
  `boat-compose.service`). **Confirmed on hardware:** `linuxserver/firefox`
  (KasmVNC-based, not the fragile Wayland-socket-sharing approach an
  earlier draft sketched - see "Deploying an app: Firefox as the helm UI")
  pulled and ran successfully via `docker-compose up -d`.
- ✅ `boat-docker-compose-plugin` (vendored static `docker compose` v2
  binary) — the only compose client meta-virtualization packages on this
  kirkstone snapshot is v1 (`python3-docker-compose`, hyphenated
  `docker-compose`); this adds the v2 `docker compose` space-separated form
  too. **Confirmed on hardware** (as a manual `~/.docker/cli-plugins`
  install first, then baked into the recipe).
- ✅ `bash` installed and set as the default login shell for both `root`
  and `boat` (`packagegroup-boat-tools` + `EXTRA_USERS_PARAMS` in
  `boat-image.bb`).
- ✅ Networking: internet-reachable out of the box (`ping 8.8.8.8` works) via
  NetworkManager. `ssh-server-openssh` also confirmed reachable.
- ❓ `nvidia-container-toolkit` GPU access in containers — not yet tested;
  still the biggest open risk (see above).
- ❌ `fake-hwclock`, `wvkbd`, `fail2ban`, `wavemon`, `bind-utils` — not
  packaged in this project's fetched kirkstone-era layers.
- ❌ RAUC A/B updates, the `/data` partition itself, and the interactive
  build-time user/SSH-key provisioning flow — later hardening, not started.

Next: [`06-troubleshooting.md`](06-troubleshooting.md)
