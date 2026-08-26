# 05 — Phase 2: The Boat Computer Layer (`meta-boat`)

**Status: `boat-image` has been built, flashed, and booted on real Xavier NX
hardware.** Confirmed working on that build: `boat-hmi-autostart` (autologin
+ the XFCE session on tty1), `dockerd` (`docker ps` responds), networking
(internet reachable), `docker compose` v2 running a real stack, SC7 sleep and
magic-packet wake, and `boat-grow-rootfs` reclaiming a 233 GiB SSD. Not yet
confirmed: GPU access in containers via `nvidia-container-toolkit` — still
the biggest open risk, see "Open risks" and "What's built vs deferred" at the
end.

> **The helm display changed: Weston/Wayland → XFCE on Xorg.** The desktop is
> now a full XFCE desktop started on an Xorg server (see "HMI / XFCE
> autostart"), and it has since been booted: the greyed-out Shut Down /
> Restart / Suspend buttons that `polkit` fixed, the `jtop` group membership,
> and the container file-ownership finding behind the i2c/spi gid pinning all
> came out of running it. Everything else in this document — Docker, the
> container design, networking, `/data` — is unchanged.

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
`boat-docker-config`/`boat-hmi-autostart`/`boat-compose`/`boat-power`/`boat-grow-rootfs`/`boat-firefox`/`boat-docker-compose-plugin`
recipes. Grouped so
you can trim it — package names below were cross-checked against this
project's actual fetched layers (kirkstone), not guessed:

| Sub-group | Packages |
|-----------|----------|
| `-containers` | `docker-ce` (meta-virtualization's default `virtual/docker` provider — `docker-moby` is a valid alternative but gets skipped as a runtime target unless you override the preference), `boat-docker-compose-plugin` (the **v2** `docker compose` CLI plugin — this is the one to use), `python3-docker-compose` (the v1 hyphenated client, kept only because meta-virtualization packages it; it is broken on this image, see below), `ca-certificates` |
| `-nvidia-container` ⚠️ | `nvidia-container-toolkit` (pulls in `libnvidia-container-tools` + `tegra-configs-container-csv`) — **unproven on kirkstone, prototype first, see risks** |
| `-nvidia-host` | `tegra-argus-daemon` (CSI cameras). Tegra userspace driver libs (`tegra-libraries-*`) are already pulled in by the BSP, not listed again |
| `-cuda` | `cuda-toolkit` (CUDA 11.4 — `nvcc`, cudart, cuBLAS/cuFFT/cuRAND/cuSOLVER/cuSPARSE/NPP, nvrtc, cuDLA), `cudnn` (8.6.0), `tensorrt-core` + `tensorrt-plugins` + `tensorrt-trtexec` (8.5.2), `python3-tensorrt`, `opencv` + `python3-opencv` (CUDA-accelerated — see the recipe's comment on why this beats JetPack's own build), `libnvvpi2` (VPI 2, the only route to the PVA/VIC engines), `tegra-mmapi-dev` — JetPack's "SDK Components" half, fetched from NVIDIA's public Jetson deb feed. Several GB; drop it, or swap `cuda-toolkit` for `cuda-libraries`, if the host only ever runs containers |
| `-jetson` | `tegra-nvpmodel`, `tegra-nvfancontrol`, `tegra-tools` (`jetson_clocks`/`tegrastats`), `python3-jetson-stats` (jtop) |
| `-connectivity` | `networkmanager` (`nmcli` comes with it) + `networkmanager-nmtui` (the TUI *is* a separate package — without it there is no console Wi-Fi picker), `modemmanager`, `avahi-daemon`+`avahi-utils`, `bluez5`, `hostapd`, `dnsmasq`, `iw`, `wireless-regdb-static`, `wireguard-tools`, `chrony` |
| `-hmi` | `packagegroup-core-x11-xserver` (expands to meta-tegra's own `XSERVER`: `xserver-xorg` + NVIDIA's `xserver-xorg-video-nvidia`), `packagegroup-xfce-base` (xfwm4, xfce4-session, xfce4-panel, xfdesktop, xfce4-settings, thunar, xfce4-terminal, …), `xinit`, `xauth`, `xrandr`, `xset`, `xdpyinfo`, `ttf-dejavu-sans`, `polkit` (what lets the desktop user reboot/shut down/suspend), `network-manager-applet` + `blueman` (the Wi-Fi and Bluetooth tray applets — both autostart via `/etc/xdg/autostart` into the panel systray) — browsers/apps themselves are containers, not packages |
| `-reliability` | `watchdog` (not `watchdog-keepalive` too — upstream declares them mutually exclusive alternatives) |
| `-security` | `openssh`, `nftables`, `sudo` |
| `-nettools` | `iproute2`, `net-tools`, `iputils`, `bmon`, `tcpdump`, `mtr`, `traceroute`, `ethtool`, `iftop`, `curl`, `nmap`, `libqmi`/`libmbim` (cellular debug) |
| `-tools` | `nvme-cli`, `parted`, `gptfdisk`, `e2fsprogs-resize2fs` (see [Reclaiming the rest of the SSD](#reclaiming-the-rest-of-the-ssd)), `i2c-tools`, `usbutils`, `pciutils`, `htop`, `tmux`, `rsync`, `nano`, `minicom`, `git` (for `/data/compose`, separate from the git inside any container), `iperf3`, `bash` |

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
  `DISTRO_FEATURES:append = " virtualization opengl pam x11 polkit"` in
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
  `wayland` was dropped from that append along with Weston — but note that
  poky's own `POKY_DEFAULT_DISTRO_FEATURES` still carries it, so it remains in
  `DISTRO_FEATURES` and GTK still builds its Wayland backend. Dropping it from
  the append only stops this project asserting a dependency it no longer has;
  actually removing it would need `DISTRO_FEATURES:remove = "wayland"`, which
  is a larger change and is not made here.
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
- **Suspend-to-RAM** (`boat-power.cfg`): `CONFIG_SUSPEND`/`PM_SLEEP` — what
  puts `mem` in `/sys/power/state` — plus `PM_DEBUG`/`PM_SLEEP_DEBUG` for
  per-device suspend/resume timing when a board won't sleep or won't come
  back. See [Power](#power-wake-on-lan-and-remote-sc7-suspend). Nothing is
  needed for the Ethernet side of Wake-on-LAN: the NIC driver is already in
  the BSP defconfig and carries its own magic-packet support.
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
  boat to update settings, then `docker compose up -d`. This is why `git` is
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
# read its header first, then edit: pin the image digest, and decide whether
# you really want the privileged/docker.sock grants it ships with
systemctl start boat-compose        # or: docker compose -f /data/compose/docker-compose.yml up -d
```

Put `/data/compose` under git for versioned, pull-to-update config. Note the
CLI verb: it is `docker compose` (a **space**, the v2 plugin), not the
hyphenated v1 `docker-compose`. This project's kirkstone-era
meta-virtualization only packages the Python-based v1 client, and CONFIRMED ON
HARDWARE it fails on this image with `ModuleNotFoundError: No module named
'distutils'` — so `boat-docker-compose-plugin` vendors the official static v2
binary, and that is what `boat-compose-up` runs. The examples shipped under
`/usr/share/boat/compose-examples/` are written for v2 as well: none of them
carries a `version:` key, which v1 would reject outright.

The Signal K image expects the **host** to provide dbus/avahi/bluez, time,
and device access (it detects a mounted host D-Bus socket and then skips its
own avahi — see the `startup.sh` in `signalk-server-dockers`).

The block below is a **minimal illustration** of that contract, not the file
you copied. `signalk.yml.example` is the operator's own running stack, and it
is considerably wider: `privileged: true`, `/dev:/dev/hostdev`, and
`/var/run/docker.sock` mounted in — which together give the container
root-equivalent control of the host. Its header explains why (it manages
sibling containers) and what to drop if yours does not. Read that header
before deploying it; Signal K itself needs none of those to read sensors over
the mounts shown here.

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

Note there is no `version:` key, deliberately: Compose v2 does not want one,
and this image's compose client is v2.

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
    image: nvcr.io/nvidia/deepstream:6.3-samples-multiarch   # CHECK THIS TAG
    runtime: nvidia                # or rely on Docker's default-runtime
    network_mode: host
    volumes:
      - /tmp/argus_socket:/tmp/argus_socket             # CSI camera (Argus)
      - /data/models:/models
    # Uncomment only if a USB camera is actually attached - an unconditional
    # `devices:` entry makes `docker compose up` fail outright on a board
    # without one.
    # devices:
    #   - /dev/video0
    restart: unless-stopped
```

**Check the image tag against NGC before using it.** NVIDIA retired the
separate `deepstream-l4t` repository after 6.2 in favour of multi-arch tags on
`nvcr.io/nvidia/deepstream`, and the DeepStream release has to match the L4T
the host runs — this project builds **R35.6.4 / JetPack 5.1.6**, while the 6.3
images were cut against JetPack 5.1.2 (R35.4.1). Browse
<https://catalog.ngc.nvidia.com/> for the tag that matches, rather than
trusting the one written here.

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
X11 route below — `browser.yml.example` does exactly that with Firefox.)

```yaml
services:
  firefox:
    image: lscr.io/linuxserver/firefox:latest
    container_name: firefox
    security_opt:
      - seccomp=unconfined   # optional, quiets some sandbox syscall warnings
    environment:
      - PUID=2000        # the "boat" user, so the profile is manageable
      - PGID=2000        # from the desktop session without sudo
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

### A browser on the boat's own screen

No browser is built into this image, and none needs to be. Firefox is not
packaged for Yocto in any layer this project fetches; building it from source
on kirkstone would mean adding meta-browser plus a matching Rust/cbindgen
toolchain, and the only natively packaged browser available (`epiphany`)
drags in a WebKitGTK build that is one of the largest compiles in all of
Yocto.

**The native answer: `boat-install-firefox`.** Mozilla publishes official,
current **aarch64** Linux builds — `os=linux64-aarch64` on their download
redirector (note it is *not* `linux-aarch64`, which 404s). The command
downloads one, verifies it against Mozilla's published `SHA256SUMS`, installs
it to `/opt/firefox` and adds an XFCE menu entry:

```bash
boat-install-firefox              # report installed vs latest, change nothing
sudo boat-install-firefox --install
```

Re-run it later to upgrade; `--remove` uninstalls and leaves `~/.mozilla`
profiles alone. It is not run at build or first boot on purpose — `do_rootfs`
has no business reaching the internet, and a first-boot unit would block on
DNS on a boat that may have no uplink for weeks. **So the browser is not in
the flashed image; run the command once when there is a connection.**

This needs no extra packages. Every shared library Mozilla's binaries link
against — gtk3/gdk, pango, cairo, atk, gdk-pixbuf, fontconfig, freetype,
alsa, dbus-1, libstdc++ and the X11 set — is already present via the XFCE
desktop; NSS, NSPR and sqlite are bundled inside the tarball. Confirmed by
`readelf`ing `firefox` and `libxul.so` against the built rootfs.

Why not a Yocto recipe: meta-browser's `meta-firefox` still carries only
`firefox_68.9.0esr` on kirkstone — **EOL since August 2020** — and wants
meta-clang plus `python2.7` on the build host. A browser with six years of
unpatched CVEs is the wrong thing to put on a boat that sits on marina wifi.

The trade: the browser is a prebuilt binary Yocto did not build, so it sits
outside the image's reproducibility and license manifests. And be precise
about what the checksum buys — `SHA256SUMS` is fetched from the same host,
over the same TLS connection, as the tarball, so it catches a corrupt or
truncated download and a stale CDN edge, but not anyone who can serve content
for that host. Mozilla also publishes `SHA256SUMS.asc`, signed with the
Mozilla Software Release key; verifying that with `gpgv` against a pubkey
shipped in the recipe is what would turn this into an authenticity check, and
is the obvious next step. Today the trust anchor is TLS to mozilla.net and
nothing more.

There are also two container answers, useful for different reasons:

| | `firefox.yml.example` | `browser.yml.example` |
|---|---|---|
| Renders | its own desktop over KasmVNC on `:3000` | directly on the HDMI screen |
| Reach it from | any phone/tablet/laptop on the LAN | the XFCE session itself |
| Needs a browser already? | **yes** — to open `:3000` | no |

So `firefox.yml.example` is the right answer for the helm UI on other
devices, and the wrong one if the Jetson's own screen has no browser yet —
you would need a browser to open it.

`browser.yml.example` closes that loop. It runs Firefox as an ordinary
windowed application on the local X display, using the same socket +
`DISPLAY` + MIT-MAGIC-COOKIE wiring described under "Container GUI apps on
the HDMI screen (X11)". Build the image once from the shipped Dockerfile:

```bash
mkdir -p /data/browser
cp /usr/share/boat/compose-examples/Dockerfile.firefox.example /data/browser/Dockerfile
docker build -t boat-firefox /data/browser

# The profile directory has to exist AND be owned by uid 2000 before the
# container starts. Docker creates a missing bind-mount source itself, as
# root - and the container runs Firefox as uid 2000, which then cannot write
# its own profile. This chown is not optional.
mkdir -p /data/browser/profile
chown 2000:2000 /data/browser/profile

cp /usr/share/boat/compose-examples/browser.yml.example /data/compose/docker-compose.yml
systemctl start boat-compose
```

It wraps Debian's maintained arm64 `firefox-esr` .deb and runs as uid 2000,
so the profile on `/data/browser/profile` is owned by the `boat` user.

> **Not yet confirmed on hardware.** The X11 plumbing — socket, `DISPLAY`,
> cookie mount, `shm_size` — has been exercised on this board with a
> containerised browser and works; Debian's firefox-esr on it has not.
> `docker logs firefox` shows X authorization failures if the cookie mount is
> wrong.

### Container file ownership

Containers write to bind-mounted volumes with raw numeric uid/gid — there is
no translation, because `daemon.json` sets no `userns-remap`. So whatever uid
the image runs its app as lands on `/data` verbatim, and `ls` on the host then
resolves those numbers against the *host's* tables.

That bites in two ways worth knowing:

- Most images default to **uid/gid 1000** (`node`, `ubuntu`, `debian`, most
  `-slim` bases). No account on this image has uid 1000 — `boat` is 2000 — so
  such files show a bare numeric owner. Set `PUID`/`PGID` (linuxserver images)
  or `user:` (plain images) to `2000:2000` where you want the desktop user to
  own them.
- gid 1000 used to be **`i2c`** here, because `boat-image.bb` created the i2c
  and spi groups without pinning their ids and `groupadd` took the first free
  one at/above `GID_MIN`. Container-written files were therefore group-owned
  by a hardware-access group, so anything later added to `i2c` to reach
  `/dev/i2c-*` also got group access to all of it. Both groups are now pinned
  into the system range (990/989), leaving 1000/1001 unclaimed. **Not
  retroactive** — files already written under gid 1000 need a `chgrp` sweep.

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
| `startx` + XFCE output | `~boat/.local/share/boat-xfce-session.log` |
| Is the session even up? | `loginctl` — expect one session for `boat` on `seat0`, `tty1` |

**Keyboard layout** is set by `/etc/X11/xorg.conf.d/10-boat-keyboard.conf`
(`XkbLayout`, default `fi`), installed by this same recipe from
`BOAT_HMI_XKB_LAYOUT`. It replaces the `keymap_layout` weston.ini setting the
Weston session used, and like that one it's read when the X server starts, so
changing it on a running device needs the session restarted. The display
driver config itself is L4T's own `/etc/X11/xorg.conf`, shipped by
meta-tegra's `tegra-configs-xorg` (pulled in by `xserver-xorg-video-nvidia`)
— don't hand-write one.

**Shut down / reboot / suspend from the panel menu work**, and it took two
things to get there. `polkit` has to be installed (it is, via
`packagegroup-boat-hmi`) *and* be in `DISTRO_FEATURES` (it is, via
`scripts/02-configure-build.sh`) — poky gates systemd's own polkit support on
that distro feature, so without it `logind` is built `-Dpolkit=false` and has
no mechanism to say yes to an unprivileged caller at all. CONFIRMED ON
HARDWARE: before that, the XFCE Log Out dialog came up with Shut Down /
Restart / Suspend greyed out and only Log Out usable, and `sudo systemctl
reboot` was the only way to restart the machine. polkit's stock rules for
`org.freedesktop.login1.*` allow an *active local session* — which the tty1
autologin session is — to do all three without a password.

One thing to re-test after any change here: enabling the feature also moves
NetworkManager from `-Dpolkit=false` (no authorization checks at all) to real
per-action checks. Its defaults also allow active sessions, so `nm-applet`
keeps working, but that is the part that depends on the session being on
`seat0`.

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
[`browser.yml.example`](../layers/meta-boat/recipes-boat/compose/files/browser.yml.example)
runs Firefox this way, as an ordinary window on the boat's own HDMI screen.

Two findings from doing this on real hardware apply to any browser container
here, not just that one. `shm_size` must be raised: Docker's default 64MB
/dev/shm is too small for a browser's renderer/GPU shared memory, and the
symptom is a window that is black but alive — the process runs fine and
nothing ever composites. And if the image's own `ENTRYPOINT` ends in
`--headless` (several do), a plain `command:` only appends arguments after
it, so the browser loads the page and exits immediately in a crash-loop with
`ExitCode=0`; the entrypoint has to be overridden, not extended.

A third fix is now **gone**: under Weston's minimal `desktop-shell`,
`--start-maximized` was ignored and the old example resorted to a synthetic
mouse click at a hardcoded screen coordinate. `xfwm4` is a full EWMH window
manager, so `--start-maximized` — and `wmctrl`/`xdotool` window-state
requests from other processes — should simply work.

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

## Power: Wake-on-LAN and remote SC7 suspend

A boat computer spends most of its life on the ship's battery with nobody at
the helm. **SC7** is Tegra's deep-sleep state — the platform state Linux
reaches through ordinary suspend-to-RAM (`mem` in `/sys/power/state` with
`deep` selected in `/sys/power/mem_sleep`): DRAM in self-refresh, CPU
clusters and the GPU off, wake handled by the always-on domain. RAM keeps
its contents, so resume is seconds rather than a full boot, and every
container comes back where it was instead of being pulled and restarted.

The pair that makes that usable remotely is *sleep on command* and *wake
over the network*, and the second one is the half that must never be
assumed: a board asleep with no wake path is a board someone has to visit.

[`boat-power`](../layers/meta-boat/recipes-boat/power/boat-power.bb) ships
both:

| What | Where |
|---|---|
| `boat-sleep` | `/usr/bin` — validate, then suspend to SC7. Meant to be run over SSH |
| `boat-wol-arm` | `/usr/bin` — arm magic-packet wake on the configured interface(s) and report what the driver actually accepted |
| `boat-wol.service` | arms it at boot (`multi-user.target`) |
| system-sleep hook | `${systemd_unitdir}/system-sleep/boat-power` — re-arms around *every* suspend, whatever triggered it |
| `90-boat-wol.conf` | `/etc/NetworkManager/conf.d/` — NM's own `ethernet.wake-on-lan` connection default (the value is `64`, the numeric MAGIC flag: NM parses connection defaults for this property as an integer, so the keyword `magic` silently fails to parse and leaves the flag alone) |
| `90-boat-wol` dispatcher | `/etc/NetworkManager/dispatcher.d/` — **the mechanism this actually relies on**; see "Keeping the flag armed" |
| `/etc/default/boat-power` | the knobs (interfaces, the interlock, delay, watchdog) |

Plus a `boat-power.cfg` kernel fragment (`CONFIG_SUSPEND`/`PM_SLEEP` and the
PM debug knobs) alongside the Docker one, and
[`../scripts/wake-boat.sh`](../scripts/wake-boat.sh) on the *build host* as
the sender.

### Putting it to sleep

```bash
ssh root@boat boat-sleep --status   # is SC7 reachable, is WoL armed, what MAC
ssh root@boat boat-sleep            # suspend to SC7
```

`boat-sleep` refuses to suspend unless at least one interface reports
magic-packet wake armed. That interlock is the point of the command
existing at all — otherwise `systemctl suspend` over SSH is one keystroke
away from a board that only a physical power cycle brings back.
`--force` overrides it (and also selects a non-`deep` sleep state if that is
all the kernel offers, and pushes past inhibitor locks);
`BOAT_SLEEP_REQUIRE_WOL=0` in `/etc/default/boat-power` turns it off for
good, which you want only on a board you can reach.

Running it over SSH is safe: `systemctl suspend` hands the request to logind
and returns, so the output and exit status reach you before the network goes
away. The connection then drops when the board actually suspends — an
expected disconnect, not a failure. It must run as **root** (arming WoL and
selecting the sleep state are both `/sys` writes) — either `ssh root@<boat>`,
or `sudo boat-sleep` as the `boat` user, which has passwordless sudo.

`boat-sleep --status` will *run* unprivileged, but it cannot answer the
Wake-on-LAN half that way and says so rather than guessing: reading the WoL
flag goes through ethtool's `ETHTOOL_GWOL` ioctl, which the kernel gates on
`CAP_NET_ADMIN`, and an unprivileged `ethtool eth0` simply omits the
`Wake-on:` lines — indistinguishable from a driver with no support. Run it
with `sudo` (or as root) to get a real answer; unprivileged it reports
`wake-on-lan: UNKNOWN` and tells you to.

### Waking it up

```bash
wol/boat-wake.sh                                  # send, then wait for it to answer
./scripts/wake-boat.sh 48:b0:2d:11:22:33          # just send the packet
BOAT_MAC=48:b0:2d:11:22:33 ./scripts/wake-boat.sh # or from the environment
wakeonlan 48:b0:2d:11:22:33                       # any WoL tool does
```

Prefer [`wol/boat-wake.sh`](../wol/boat-wake.sh). It sends the same packet —
it calls `scripts/wake-boat.sh` to build it, so there is one sender to keep
correct — and then adds the half that matters: waiting until the board
actually answers, and telling you when it does not. `wol/boat-sleep.sh` is
its counterpart for the sleep side. Both read the boat's MAC and address from
`wol/boat.conf` (git-ignored; copy `wol/boat.conf.example`), and so does
`scripts/wake-boat.sh` when run on its own. See
[`wol/README.md`](../wol/README.md).

A magic packet is one unacknowledged UDP datagram (port 9) carrying
`ff:ff:ff:ff:ff:ff` followed by the target MAC sixteen times — nothing
answers it, so "it did nothing" and "it never arrived" look identical from
the sender. Two things decide whether it arrives:

- **Same layer-2 segment.** `255.255.255.255` is never routed. From another
  subnet you need that subnet's directed broadcast (`192.168.1.255`) *and* a
  router willing to forward directed broadcasts — most aren't. From ashore,
  wake through a **VPN endpoint on the boat's LAN** (`wireguard-tools` is on
  the image for this) rather than by forwarding UDP 9 from the internet: a
  port-forward to a broadcast address is both unreliable and an open wake
  button for anyone who finds it.
- **The switch still knows the port.** A board asleep for hours may have
  aged out of the switch's MAC table, turning the "directed" broadcast into
  a flood — which is fine — but a managed switch with port security may
  drop it instead. `wake-boat.sh` sends three packets for the same reason.

### Keeping the flag armed

Wake-on-LAN is a per-interface flag that is easy to lose, so it is set in
four places rather than one — and the fourth is the one that makes it stick:

1. **`boat-wol.service`** at boot, via `ethtool -s eth0 wol g`, plus
   `/sys/class/net/eth0/device/power/wakeup` so the bus glue is allowed to
   wake the system too. Ordered `After=network-online.target`: CONFIRMED ON
   HARDWARE, the `nvethernet` driver reports no Wake-on-LAN support at all
   until the PHY attaches at link-up, which on a real boot was 4.4 seconds
   after this unit first ran and failed.
2. **NetworkManager's own default** (`90-boat-wol.conf`). NM re-applies the
   connection profile's WoL setting on every activation — a cable replug, a
   profile edit, a resume — so without this it would quietly clear what
   `ethtool` set at boot. Note it is written as `ethernet.wake-on-lan=64`,
   not `magic`: NM reads *connection defaults* for this property with an
   integer parser, and an unparseable value makes it leave the flag alone.
3. **The system-sleep hook**, on both sides of every suspend: immediately
   before (last chance) and immediately after (drivers that re-probe the MAC
   on resume come back with `wol=d`).
4. **The NetworkManager dispatcher script**
   (`/etc/NetworkManager/dispatcher.d/90-boat-wol`) — and this is the durable
   fix, not a belt-and-braces extra. CONFIRMED ON HARDWARE: on a real
   suspend/resume cycle NM tore the interface down **41 milliseconds** after
   the sleep hook armed it, then brought it back applying the profile's own
   setting and cleared the flag. Anything that arms WoL *before* NM finishes
   is racing a fight it will lose. A dispatcher script cannot lose that race,
   because NM calls it after the activation it would otherwise clobber — which
   covers boot, resume from SC7, and a cable replugged mid-voyage in one
   mechanism. Items 1-3 are the early-boot and belt-and-braces effort;
   this is what keeps the flag set.

**"Armed" means both halves**, and `boat-wol-arm` reads both *back* from
sysfs rather than trusting either write: the MAC's `Wake-on: g`, and — where
the device exposes one — its bus wake source reading `enabled`. A NIC that
took the magic-packet flag while its wake source stayed disabled does not
count, because the packet would reach a NIC whose wake signal goes nowhere;
a device with no such attribute is normal, and then the NIC flag is all
there is to check. `boat-wol-arm` exits non-zero when nothing is armed,
which is what makes `systemctl status boat-wol` red on hardware that cannot
do magic-packet wake at all, and what `boat-sleep`'s interlock reads — one
definition of "armed", not two that drift. `boat-wol-arm --check` runs the
same test writing nothing, which is how `--status` can promise it changes
nothing. That red is the useful signal; check it before trusting the first
remote `boat-sleep`.

### `/etc/default/boat-power`

| Variable | Default | Meaning |
|---|---|---|
| `BOAT_WOL_INTERFACES` | `eth0` | interfaces to arm (space separated). Build-time default comes from `BOAT_WOL_INTERFACES` in the recipe, which also feeds the NM `match-device` line |
| `BOAT_SLEEP_REQUIRE_WOL` | `1` | refuse to suspend with no wake path |
| `BOAT_SLEEP_DELAY` | `3` | seconds before logind is asked, so an SSH session closes cleanly |
| `BOAT_SLEEP_STOP_WATCHDOG` | `0` | stop/start `watchdog.service` around the sleep |

### What has and has not been verified on hardware

Verified on a Xavier NX devkit: the board sleeps to SC7 and comes back on a
magic packet. The specifics that came out of those runs are recorded where
they matter rather than here — the 4.4 s PHY-attach gap in
`boat-wol.service`, the 41 ms NetworkManager teardown in
`boat-wol-dispatcher.sh`, and the driver name (`nvethernet`) in
`boat-wol-arm.sh`. `wol/README.md` has a timestamped trace of a full
sleep-and-wake cycle.

Still to confirm on your own bench, because they are properties of the board
and its wiring rather than of this layer:

- **Your** devkit's Ethernet. The answer is in the driver + PHY + carrier
  board: the MAC must keep the `g` flag, the PHY must stay powered through
  SC7, and the wake signal must be wired to the always-on domain. `ethtool -i
  eth0` names the driver and `boat-wol-arm` reports what it offers — believe
  the board, not this document.
- **That SC7 comes up on your BSP.** `boat-sleep --status` should report a
  `deep` state. If `/sys/power/mem_sleep` offers only `s2idle`, suspend would
  be a shallow idle loop instead — worth knowing *before* the boat is
  unattended.
- **The hardware watchdog.** `watchdog` (in `-reliability`) feeds the Tegra
  watchdog; whether that timer keeps counting across SC7 and resets the
  board mid-sleep is untested. A board that reboots itself a minute into
  every sleep is that symptom — set `BOAT_SLEEP_STOP_WATCHDOG=1`.
- **Docker across suspend/resume.** Containers keep running through a
  suspend, but anything holding a network connection (an MQTT bridge, a
  registry pull) sees it drop and has to reconnect on resume.

Deliberately not implemented: **no RTC wake alarm** (`rtcwake` needs a
working RTC and the devkit has none — see [Time](#time-without-a-gps-or-rtc)),
and **no scheduled sleep**. Both belong on a board that has an RTC fitted;
until then the wake path is the network, and only the network.

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

## Reclaiming the rest of the SSD

The image is flashed with a **fixed-size** root filesystem —
`ROOTFS_SIZE_BYTES` in [`../scripts/env.sh`](../scripts/env.sh), 16 GiB by
default — because nothing at build time knows how big the boat's SSD is, and
because `make-sdcard` writes that whole size over recovery-mode USB 2.0 with a
plain non-sparse `dd`. Keeping it small is what makes flashing quick; this
command is what makes the drive fully usable afterward. On a normal flash
the partition already covers the whole SSD and only the filesystem inside it
is short, so the job is a single online `resize2fs` — see below.

`boat-grow-rootfs` (from the `boat-grow-rootfs` recipe) reclaims it, run
once from a terminal on the desktop after the first boot:

```bash
sudo boat-grow-rootfs            # report only — this is the default
sudo boat-grow-rootfs --grow     # actually grow (asks to confirm)
```

**There are two different gaps, and normally only one of them applies.**
`make-sdcard` creates the last partition with a "fill to end" flag, so after
a flash the APP *partition* already spans the whole SSD — what is undersized
is the *filesystem inside it*. On a 233 GiB drive that means a 231.8 GiB
partition holding a 16 GiB ext4, and the entire job is one online
`resize2fs`, with no partition table change at all.

The other gap — unallocated space *after* the partition — only shows up if
the partition itself is short (a hand-made layout, a restored image). For
that case the command moves the GPT backup header to the true end of the disk
(`sgdisk --move-second-header`), extends the partition (`sfdisk -N`, which
keeps its type GUID, PARTUUID and name — the bootloader depends on those),
re-reads just that partition's size (`partx -u`), and only then resizes the
filesystem. It reports both gaps separately so it is obvious which one you
have. `/` stays mounted throughout either way.

This is safe because **`APP` is the last partition** in the NVMe layout
meta-tegra flashes — `kernel`, `kernel-dtb`, the A/B chain reserves,
`recovery`, `RECROOTFS`, `esp`/`esp_alt`, `UDA`, then `APP` (see
`yocto/flash/external-flash.xml.in` after an unpack). Growing it therefore
only ever claims space that is already free: nothing is moved and no file
data is rewritten. The script verifies that itself rather than trusting the
layout, and refuses if any partition is allocated past the rootfs — which is
exactly what a hand-added `/data` partition would be. It also saves the
original partition table to `/var/lib/boat/gpt-backup-<disk>.bin` before
touching anything.

**Decide about `/data` first.** `--grow` extends the rootfs partition to the
last usable sector, so once it has run there is no unallocated space left to
carve a separate `/data` partition out of — and `/data` is where
`daemon.json` points Docker's `data-root` and where `boat-compose` looks for
your stack. The two shipped features are in tension and this is the moment
that decides between them:

- **Everything on one filesystem** (what happens today if you just run
  `--grow`): `/data` stays a directory on the grown rootfs. Simple, and fine
  for a bench or a boat you reflash rarely. A read-only root is then off the
  table.
- **A separate `/data`**: create that partition *before* growing, then run
  `boat-grow-rootfs` — it will refuse to extend the partition (correctly: a
  partition is now allocated past the rootfs) and do the filesystem-only half
  of the job, which is the half that matters after a normal flash anyway.

Deliberately **not** a first-boot systemd unit: rewriting a partition table
is the one operation on this image that can lose the whole rootfs if the
disk isn't what was expected, and doing it unattended before anyone has
looked at the machine buys nothing.

**Verified on hardware** (Xavier NX devkit, 233 GiB NVMe, R35.6.4):

```
partition   /dev/nvme0n1p1 (no. 1)  231.8 GiB
filesystem  ext4, 16.0 GiB - 4.3G used of 14.8G
unallocated after the partition:      0.0 B
filesystem short of its partition by: 215.8 GiB
...
resize2fs 1.46.5 — The filesystem on /dev/nvme0n1p1 is now 60764713 (4k) blocks long.
done - / is now 217.1G
```

Re-running afterwards correctly reports "nothing to do".

## Reliability

- `watchdog` tied to the Tegra hardware watchdog.
- **Read-only root + `overlayfs`** (or a dedicated writable **`/data`
  partition**) so an abrupt power cut can't corrupt the rootfs. Container
  volumes and Docker's `data-root` are meant to live on `/data` — but that
  partition isn't provisioned by any recipe here yet, so until it is,
  `boat-image`'s rootfs has extra headroom
  (`IMAGE_ROOTFS_EXTRA_SPACE = "4194304"`) as a stopgap — and on an SSD
  larger than 16 GiB, [`boat-grow-rootfs`](#reclaiming-the-rest-of-the-ssd)
  turns the unallocated remainder into rootfs space.
- Periodic `fstrim` on the NVMe; size-cap persistent journald.

## Open risks — prototype these first

The image builds, flashes and boots (see the status note at the top). What is
listed here is what those boots did *not* settle:

1. **`nvidia-container-toolkit` on meta-tegra kirkstone.** This is the
   linchpin for GPU-in-container and the least certain piece. Validate a
   DeepStream container seeing the GPU **before** committing to the rest.
2. **DLA / ISP exposure into the container** (accelerator visibility, not
   just the GPU).
3. **CSI camera via `nvargus-daemon` socket** across the container boundary.
4. **Image size / partition growth** — Docker `data-root` on NVMe, and the
   `/data` partition itself doesn't exist yet.

5. **Unprivileged Xorg on the Tegra X driver** — it comes up, but it is the
   least conventional part of this image and worth understanding before you
   change anything around it. `boat-hmi-autostart` starts Xorg as the `boat` user,
   relying on `systemd-logind` for DRM master and input devices; L4T's own
   Ubuntu images run X as root under a display manager instead. The
   NVIDIA `nvidia_drv.so` also wants `/dev/nvhost-*` and `/dev/nvmap`, which
   meta-tegra's udev rules give to group `video` (`boat` is a member). If the
   first boot comes up black, `~boat/.local/share/xorg/Xorg.0.log` says which
   of those two it was; running the same `startx` line as root from tty1 is
   the quick way to confirm it's a privilege problem rather than a driver one.

6. **The hardware watchdog across SC7.** Sleep and magic-packet wake both
   work on this board (see
   [Power](#power-wake-on-lan-and-remote-sc7-suspend)); what is still untested
   is whether the Tegra watchdog keeps counting through SC7 and resets the
   board mid-sleep. The symptom is a board that reboots itself a minute into
   every sleep — set `BOAT_SLEEP_STOP_WATCHDOG=1` if you see it. On a
   *different* board, re-check the first two as well: the interlock in
   `boat-sleep` means an unsupported PHY refuses to sleep rather than
   stranding the machine, but a remote-sleep workflow you can't use is still a
   workflow you don't have.

7. **`/data` is not provisioned by anything.** `daemon.json` points Docker's
   `data-root` at `/data/docker` and `boat-compose` reads `/data/compose`, but
   no recipe creates that partition or mount. Today dockerd simply creates the
   directory on the rootfs, which works and quietly spends the space the
   16 GiB flashed rootfs was meant to keep clear — hence
   `IMAGE_ROOTFS_EXTRA_SPACE` and the log-size caps in `daemon.json`.
   `boat-docker-config` ships a `docker.service` drop-in with
   `RequiresMountsFor=/data` so dockerd waits for the mount rather than
   racing it on the day it appears; and see
   [Reclaiming the rest of the SSD](#reclaiming-the-rest-of-the-ssd), because
   `boat-grow-rootfs --grow` takes the space a `/data` partition would need.

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
  on `/data`, json-file logs capped at 3 × 10 MB; plus a `docker.service`
  drop-in with `RequiresMountsFor=/data`). The `/data` mount itself is still
  not provisioned. **Confirmed on hardware:** `dockerd` starts and `docker ps`
  responds.
- ✅ `boat-hmi-autostart` (fixed `boat`/UID 2000 autologin on tty1, then
  `startx` → `xfce4-session`, see "HMI / XFCE autostart"). No
  touchscreen-specific calibration wired up. **Confirmed on hardware** in its
  XFCE/Xorg form — the `polkit` and `jtop`-group findings recorded elsewhere
  in this document both came out of running it.
- ✅ X11 for containerized GUI apps — now the desktop's own X server rather
  than XWayland, and with a per-container mounted MIT-MAGIC-COOKIE instead of
  the Weston-era blanket `xhost +local:` grant. See "Container GUI apps on
  the HDMI screen (X11)".
- ✅ `boat-compose` (example compose files including `x11-app.yml.example` +
  `boat-compose.service`). **Confirmed on hardware:** `linuxserver/firefox`
  (KasmVNC-based, so it never touches the host display - see "Deploying an
  app: Firefox as the helm UI")
  pulled and ran successfully via `docker compose up -d`.
- ✅ `boat-docker-compose-plugin` (vendored static `docker compose` v2
  binary) — the only compose client meta-virtualization packages on this
  kirkstone snapshot is v1 (`python3-docker-compose`, hyphenated
  `docker-compose`), and CONFIRMED ON HARDWARE that one fails on this image
  with `ModuleNotFoundError: No module named 'distutils'`. This is therefore
  the compose client to use, not an alternative: `boat-compose` depends on it
  and `boat-compose-up` execs it. **Confirmed on hardware** (as a manual
  `~/.docker/cli-plugins` install first, then baked into the recipe).
- ✅ `boat-power` (Wake-on-LAN armed at boot, on both sides of every
  suspend, by NetworkManager's own default and — the mechanism that actually
  holds — by a NetworkManager dispatcher script; `boat-sleep` for remote SC7
  suspend with a refuse-to-sleep-without-a-wake-path interlock;
  `wol/boat-sleep.sh` / `wol/boat-wake.sh` on the host, over
  `scripts/wake-boat.sh` as the sender). **Confirmed on hardware:** a full
  sleep and magic-packet wake. ❓ Still open: whether the hardware watchdog
  keeps counting across SC7, see "Power".
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
- ⚠️ **Credentials.** Root and `boat` both have an *empty* password, sshd
  accepts root logins and empty passwords, and `boat` has passwordless sudo
  and `docker` group membership (root-equivalent). Anyone who can reach port
  22 has root with no credential. Deliberate for a bench image, declared
  explicitly in `boat-image.bb` rather than inherited from `debug-tweaks` —
  and the thing to reverse first before this goes near an untrusted network.
  See "Build-time user & SSH" above.

Next: [`06-troubleshooting.md`](06-troubleshooting.md)
