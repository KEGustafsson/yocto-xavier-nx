# 05 — Phase 2: The Boat Computer Layer (`meta-boat`)

**Status: `boat-image` has been built, flashed, and booted on real Xavier NX
hardware.** Confirmed working on that build: `boat-hmi-autostart` (autologin
+ a graphical session on tty1), `dockerd` (`docker ps` responds), and
networking (internet reachable). Not yet confirmed: GPU access in containers
via `nvidia-container-toolkit` — still the biggest open risk, see "Open
risks" and "What's built vs deferred" at the end.

> **The helm display changed: Weston/Wayland → XFCE on Xorg.** The confirmed
> boot above was the old Weston session. The desktop is now a full XFCE
> desktop started on an Xorg server (see "HMI / XFCE autostart"), which has
> **not** been re-validated on hardware yet. Everything else in this document
> — Docker, the container design, networking, `/data` — is unchanged. If you
> are reading this after a successful XFCE boot, please update this note.

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
        │  Xorg + XFCE desktop (GPU)   nvpmodel / jetson-clocks / nvfancontrol│
        │  openssh / nftables          watchdog                              │
        └───────────────┬───────────────────────────────────────────────────┘
                        │  docker-compose (files on /data)
        ┌───────────────┴───────────────────────────────────────────────────┐
        │  CONTAINERS (you pull / compose)                                    │
        │   • signalk-server  (your GHCR / Docker Hub, arm64)                 │
        │   • deepstream-l4t  (nvcr.io, GPU + DLA + NVENC/NVDEC + ISP)        │
        │   • firefox         (helm UI → own KasmVNC desktop, any browser)   │
        │   • influxdb / grafana / node-red  (optional dashboards)            │
        └────────────────────────────────────────────────────────────────────┘
```

Rule of thumb: **anything that owns real hardware — the GPU driver, the
display server, the network interfaces, the system clock, the watchdog —
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
| `-hmi` | `packagegroup-core-x11-xserver` (expands to meta-tegra's own `XSERVER`: `xserver-xorg` + NVIDIA's `xserver-xorg-video-nvidia`), `packagegroup-xfce-base` (xfwm4, xfce4-session, xfce4-panel, xfdesktop, xfce4-settings, thunar, xfce4-terminal, …), `xinit`, `xauth`, `xrandr`, `xset`, `xdpyinfo`, `dbus`, `ttf-dejavu-sans` — browsers/apps themselves are containers, not packages |
| `-reliability` | `watchdog` (not `watchdog-keepalive` too — upstream declares them mutually exclusive alternatives) |
| `-security` | `openssh`, `nftables` |
| `-nettools` | `iproute2`, `net-tools`, `iputils`, `bmon`, `tcpdump`, `mtr`, `traceroute`, `ethtool`, `iftop`, `curl`, `nmap`, `libqmi`/`libmbim` (cellular debug) |
| `-tools` | `nvme-cli`, `i2c-tools`, `usbutils`, `pciutils`, `htop`, `tmux`, `rsync`, `nano`, `minicom`, `git` (for `/data/compose`, separate from the git inside any container), `iperf3` |

Not available in this project's fetched kirkstone-era layers, and
deliberately **omitted** rather than left as names that fail the build:
`fail2ban`, `wavemon`, `bind-utils`
(`dig`/`nslookup`), `fake-hwclock`. Add them from a newer layer snapshot if
you need them. See
[`../layers/meta-boat/recipes-core/packagegroups/packagegroup-boat.bb`](../layers/meta-boat/recipes-core/packagegroups/packagegroup-boat.bb)
for the authoritative, commented list.

### Layers, DISTRO_FEATURES, licensing

- **New layers:** `meta-virtualization` (Docker; needs meta-oe / meta-python /
  meta-networking / meta-filesystems, already present). meta-tegra provides
  the Tegra driver, `nvargus-daemon` and the container-runtime bits once
  meta-virtualization's `virtualization-layer` collection is present.
  `meta-tegra-community` provides `jetson-stats`. **`meta-xfce`** provides
  the helm desktop, and drags in **`meta-gnome`** and **`meta-multimedia`**
  as its own declared `LAYERDEPENDS` — bitbake refuses to parse without
  them. All three are sublayers of the `meta-openembedded` clone
  `scripts/01-fetch-layers.sh` already makes, so nothing extra is fetched;
  `scripts/02-configure-build.sh` just adds them to `bblayers.conf`.
- `scripts/02-configure-build.sh` sets
  `DISTRO_FEATURES:append = " virtualization opengl pam x11"` in
  `local.conf` (build-wide, not per-image — `DISTRO_FEATURES` gates other
  recipes' `REQUIRED_DISTRO_FEATURES` at parse time, so an image-recipe-local
  append can't retroactively unskip them). `x11` gates `xserver-xorg`,
  `packagegroup-core-x11-xserver` and every meta-xfce recipe; `opengl`
  builds GLX, which xfwm4's compositor and any GPU-accelerated app on the
  same display need. `pam` isn't cosmetic either: with systemd init,
  `pam_systemd` is what makes `systemd-logind` create a session on seat0 for
  the autologin user — without that session the **unprivileged** Xorg
  `boat-hmi-autostart` starts cannot take DRM master or open input devices
  at all.
  `wayland` was dropped from that list along with Weston; add it back if you
  reintroduce a Wayland compositor.
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

Firefox is **not** built in Yocto, and this deployment doesn't hand it the
host's display at all. Instead:
**[`linuxserver/firefox`](https://docs.linuxserver.io/images/docker-firefox/)**,
which has official `arm64v8` builds and runs its own browser-accessible
desktop (KasmVNC). This is more useful on a boat: any phone/tablet/laptop
browser on the LAN can reach the helm UI, not just whatever's plugged into
the HDMI port.

(If you *do* want a browser painting on the Jetson's own screen, that is the
X11 route below — `signalk-kiosk.yml.example` does exactly that with
Chromium.)

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

`boat-hmi-autostart`'s local XFCE/tty1 desktop is still useful on its own —
open this same KasmVNC URL (`localhost:3000`) in a local browser there if you
want the Firefox container on the HDMI screen too.

## HMI / XFCE autostart

The helm display is a full **XFCE desktop on an Xorg server**, started
automatically at boot. (Earlier versions of this image booted into a bare
Weston/Wayland session instead; see "Why not Weston any more" below.)

`boat-hmi-autostart` autologins a fixed `boat` user (UID 2000, created via
`extrausers` in `boat-image.bb`) on tty1 via a `getty@tty1.service` drop-in,
and `/etc/profile.d/boat-xfce-autostart.sh` then runs, from that login shell:

```sh
exec startx /usr/bin/boat-xfce-session -- :0 vt1
```

`/usr/bin/boat-xfce-session` (also from this recipe) is the X session's first
and only client. It exports a copy of the session's MIT-MAGIC-COOKIE to
`/run/boat-x11/Xauthority` for containerized GUI apps — the X
server is already up and `XAUTHORITY` is already set at that point, which is
why the export lives here — and then execs `dbus-run-session --
xfce4-session`, giving XFCE the session bus that `xfconf`, `xfsettingsd`,
`thunar` and `xfce4-notifyd` all need.

Boot flow: systemd autologin (`boat`, UID 2000) → `pam_systemd` creates the
logind session on seat0 → `startx` → Xorg on vt1 with NVIDIA's Tegra X driver
→ `xfce4-session` → panel, desktop, window manager.

**Why it starts from the autologin session and not `xserver-nodm-init`:**
poky's `xserver-nodm-init` is the usual "X with no display manager" unit, but
it runs Xorg **as root**, so the whole desktop and everything launched from
it would be root too, and `systemd-logind` would never create the
`/run/user/2000` session directory this image's design pins to
`BOAT_HMI_UID`. Starting X from the autologin session instead keeps the
desktop unprivileged and gives it a real seat — which is also the thing that
lets a non-root Xorg take DRM master and open input devices in the first
place (Xorg 21.1 is built here with `systemd-logind` support; poky does not
build the setuid `Xorg.wrap`, so there is no other way for a normal user to
start it). For the same reason `xserver-nodm-init` is deliberately **not**
installed: it declares `Alias=display-manager.service` and would race this
session for vt1. That is why `packagegroup-boat-hmi` pulls
`packagegroup-core-x11-xserver` rather than the broader
`packagegroup-core-x11`, whose `-utils` half would drag it in.

**Where to look when the screen stays black:**

| What | Where |
|------|-------|
| Xorg's own log | `~boat/.local/share/xorg/Xorg.0.log` (a non-root Xorg redirects there by itself; `/var/log` isn't writable) |
| `startx` + XFCE output | `/tmp/boat-xfce-session.log` |
| Is the session even up? | `loginctl` — expect one session for `boat` on `seat0`, `tty1` |

**Keyboard layout** is set by `/etc/X11/xorg.conf.d/10-boat-keyboard.conf`
(`XkbLayout`, default `fi`), installed by this same recipe from
`BOAT_HMI_XKB_LAYOUT`. It replaces the `keymap_layout` weston.ini setting the
Weston session used, and like that one it's read when the X server starts, so
changing it on a running device needs the session restarted. The display
driver config itself is L4T's own `/etc/X11/xorg.conf`, shipped by
meta-tegra's `tegra-configs-xorg` (pulled in by `xserver-xorg-video-nvidia`)
— don't hand-write one.

**Shut down / reboot from the panel menu will not work** as the `boat` user:
`polkit` is not in `DISTRO_FEATURES`, so `systemd-logind` only grants
power-off/reboot to root. Log Out works (that's `xfce4-session`'s own D-Bus
call, no privilege involved). Run `poweroff` as root instead (`su -` in
`xfce4-terminal`, or over SSH — `sudo` is not in this image), or add
`polkit` to the `DISTRO_FEATURES:append` line in
`scripts/02-configure-build.sh` — that also switches on `xfce4-session`'s own
`polkit` PACKAGECONFIG.

**Screen blanking** is X's default (blank after ~10 min, DPMS off after
~20/30 min); `xfce4-power-manager` is not installed. For an always-on helm
panel, add `-s 0 -dpms` to the server arguments in
`boat-xfce-autostart.sh`, or run `xset s off -dpms` from the session.

`BOAT_HMI_USER`/`BOAT_HMI_UID` in
[`boat-hmi-autostart.bb`](../layers/meta-boat/recipes-boat/hmi-autostart/boat-hmi-autostart.bb)
must keep matching whatever `extrausers` creates in `boat-image.bb` — they
default to `boat`/`2000` in both places.

### Why not Weston any more

The previous design ran Weston directly from the same autologin session. It
worked (confirmed on hardware) but was a bare compositor: no panel, no
launcher, no file manager, no settings UI — and every X11-only application
had to reach it through XWayland. Notes from that setup, kept because they
explain shapes still visible in the code and in git history:

- `weston-init`'s `weston-start` wrapper was unusable here: it launches
  weston through `su -c "..." $WESTON_USER`, which with `WESTON_USER` unset
  defaults to **root** and separately refuses to run unless it's the
  controlling tty's foreground process group ("su: must be run from a
  terminal"). The autostart script called `weston` directly to sidestep it —
  the current script calls `startx` directly for the analogous reason.
- Weston needed `--use-pixman` on this Jetson: with the GL/GBM renderer the
  pointer left trailing black boxes behind it. That was a compositor-side
  cursor-plane damage-tracking problem, not something Xorg + NVIDIA's Tegra
  driver goes through — but if you see cursor corruption on the XFCE desktop,
  turning off xfwm4's compositor (Settings → Window Manager Tweaks →
  Compositor) is the equivalent first thing to try.
- XWayland shipped in a **separate** `weston-xwayland` package, and weston
  crash-looped the whole console session without it. Nothing in the X11 setup
  has that failure mode; `xserver-xorg-video-nvidia` is a hard `RDEPENDS` of
  the packagegroup entry.

## Container GUI apps on the HDMI screen (X11)

Not every containerized GUI app ships a KasmVNC-style web desktop like
`linuxserver/firefox`. Plain X11 apps (OpenCPN, Chromium, …) render straight
onto the HDMI screen instead, as ordinary clients of the same X server the
XFCE desktop is running on — no XWayland bridge in the path any more. They
need three things: the X socket, `DISPLAY`, and an authorization cookie. As
the session starts, `boat-xfce-session` exports a copy of its
MIT-MAGIC-COOKIE (family rewritten to `FamilyWildcard`, so it matches from
inside a container's own UTS namespace) to:

```
/run/boat-x11/Xauthority
```

`/run/boat-x11` itself is created at boot by `systemd-tmpfiles`, from
`/usr/lib/tmpfiles.d/boat-x11.conf` (shipped by the same recipe), mode `0700`
and owned by the `boat` user.

Compose your container with:

```yaml
services:
  x11-app:
    image: <your X11 app image, arm64>
    network_mode: host
    environment:
      - DISPLAY=:0
      - XAUTHORITY=/run/boat-x11/Xauthority
    volumes:
      - /tmp/.X11-unix:/tmp/.X11-unix
      - /run/boat-x11:/run/boat-x11:ro
    restart: unless-stopped
```

Mount the *directory*, not the file: the session writes a fresh cookie file
on every restart, and a file bind-mount would keep the container pinned to
the old, deleted one. `:ro` because nothing in the container has any reason
to write it.

**This cookie is the session's own, so it is full access to the helm
display** — anything holding it can inject keystrokes and pointer events,
capture the screen and manipulate other clients' windows. Hand it only to
containers you trust with the display; there is no lesser grant here (X's
`SECURITY` extension "untrusted" cookies exist, but break GLX and most real
apps, so this image does not use them).

Earlier versions of this image ran `xhost +local:` in `boat-xfce-session`
instead, and containers needed no cookie at all. That grant turns cookie
authentication **off** for every same-machine client: any local process, of
any uid, could then keylog or take over the helm session, whether or not it
was ever given the X socket. `-nolisten tcp` does not help — it only keeps
the network out, and these clients come in over `/tmp/.X11-unix`. The
mounted-cookie scheme above grants exactly the same power, but only to the
containers that were actually handed the mount. The cookie file itself is
readable by any uid (container images often run their app as a non-root
user), which is safe because its parent `/run/boat-x11` is `0700`: no other
unprivileged uid on the host can traverse into it, and `dockerd` bind-mounts
it as root.

**Why not `/run/user/2000`**, the obvious place for a session's runtime
files: `logind` mounts a tmpfs there for each login session, and `dockerd`
silently creates a missing bind-mount source as a root-owned directory. A
container coming up before the first login would create — then stay pinned
to — the directory *underneath* that later tmpfs, and would never see a
cookie. A tmpfiles-created directory outside `/run/user` exists before
`docker.service` starts and survives session restarts.

To verify on hardware — the approved path from the `boat` session, the
rejected one from a root shell (serial console or SSH):

```sh
# approved: the exported cookie, exactly what the containers are handed
XAUTHORITY=/run/boat-x11/Xauthority DISPLAY=:0 xdpyinfo | head -3
#   name of display:    :0
#   version number:     11.0
#   ...

# unrelated local client: another uid, no cookie, and no way into
# /run/boat-x11 to find one -> rejected
su -s /bin/sh -c 'DISPLAY=:0 xdpyinfo' nobody
#   No protocol specified
#   xdpyinfo: unable to open display ":0".
```

Under the old `xhost +local:` grant that second command printed the display
information instead of failing — that is the whole difference.

See
[`x11-app.yml.example`](../layers/meta-boat/recipes-boat/compose/files/x11-app.yml.example)
for the shipped copy of this template.

**Concrete example:**
[`signalk-kiosk.yml.example`](../layers/meta-boat/recipes-boat/compose/files/signalk-kiosk.yml.example)
combines Signal K with a normal-mode (not `--kiosk`) Chromium container
(`zenika/alpine-chrome`) pointed at `http://localhost:3000`, so Signal K's
web admin UI shows up directly on the boat's own HDMI screen, full browser
chrome (tabs, address bar) included, instead of needing a browser on another
device. Two fixes in that file were confirmed on hardware under the Weston
session and are unaffected by the switch: `shm_size` (Docker's default 64MB
was a black-screen cause) and overriding the image's `--headless`
`ENTRYPOINT`. A third one is now **gone**: under Weston's minimal
`desktop-shell`, `--start-maximized` was ignored and the file resorted to a
synthetic mouse click at a hardcoded screen coordinate to maximize the
window. `xfwm4` is a full EWMH window manager, so `--start-maximized` (and
`wmctrl`/`xdotool` window-state requests from other processes) should simply
work — this is the one part of that example not yet re-confirmed on hardware.
The `signalk-server` service in that file is adapted from a known-working
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

5. **Unprivileged Xorg on the Tegra X driver** — new with the XFCE switch and
   not yet booted. `boat-hmi-autostart` starts Xorg as the `boat` user,
   relying on `systemd-logind` for DRM master and input devices; L4T's own
   Ubuntu images run X as root under a display manager instead. The
   NVIDIA `nvidia_drv.so` also wants `/dev/nvhost-*` and `/dev/nvmap`, which
   meta-tegra's udev rules give to group `video` (`boat` is a member). If the
   first boot comes up black, `~boat/.local/share/xorg/Xorg.0.log` says which
   of those two it was; running the same `startx` line as root from tty1 is
   the quick way to confirm it's a privilege problem rather than a driver one.

(The Firefox-in-container Wayland-socket-handshake risk from an earlier draft
of this doc is gone — the deployed approach, `linuxserver/firefox`, doesn't
touch the host's display at all. See "Deploying an app: Firefox as the helm
UI" above.)

## What's built vs deferred

- ✅ `packagegroup-boat` trimmed to the container-host design
  (`-containers`, `-nvidia-container`, `-nvidia-host`, `-jetson`, `-hmi`,
  `-security`, `-nettools`, updated `-connectivity`/`-reliability`/`-tools`).
- ✅ Kernel `.bbappend` with the Docker + i2c/spi/usb-serial fragment.
- ✅ `boat-docker-config` (`daemon.json`: nvidia default-runtime, data-root
  on `/data` — the `/data` mount itself is not provisioned). **Confirmed on
  hardware:** `dockerd` starts and `docker ps` responds.
- ✅ `boat-hmi-autostart` (fixed `boat`/UID 2000 autologin on tty1, then
  `startx` → `xfce4-session`, see "HMI / XFCE autostart"). No
  touchscreen-specific calibration wired up. **Confirmed on hardware:** the
  autologin-and-launch-a-session-from-tty1 mechanism, in its Weston form.
  ❓ The XFCE/Xorg form of it is written but not yet flashed and booted.
- ✅ X11 for containerized GUI apps — now the desktop's own X server rather
  than XWayland, and with a per-container mounted MIT-MAGIC-COOKIE instead of
  the Weston-era blanket `xhost +local:` grant. See "Container GUI apps on
  the HDMI screen (X11)". ❓ Not yet re-confirmed on hardware.
- ✅ `boat-compose` (example compose files including `x11-app.yml.example` +
  `boat-compose.service`). **Confirmed on hardware:** `linuxserver/firefox`
  (KasmVNC-based, so it never touches the host display - see "Deploying an
  app: Firefox as the helm UI")
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
- ❌ `fake-hwclock`, `fail2ban`, `wavemon`, `bind-utils` — not
  packaged in this project's fetched kirkstone-era layers.
- ❌ RAUC A/B updates, the `/data` partition itself, and the interactive
  build-time user/SSH-key provisioning flow — later hardening, not started.

Next: [`06-troubleshooting.md`](06-troubleshooting.md)
