# meta-boat

A small Yocto layer that turns the base Xavier NX image into a **Jetson
container host** for a marine / boat computer. It is Phase 2 of this
project — Phase 1 gets a plain image booting from NVMe first.

Applications (Signal K, DeepStream, a browser HMI, ...) are **not** baked
into the rootfs; they run as Docker containers composed from `/data/compose`
on the target. The full design is
[`../../docs/05-phase2-boat-computer-layer.md`](../../docs/05-phase2-boat-computer-layer.md)
— read that first; this README just maps the design onto the recipes here.

## What it adds

| Area | Packages / units |
|------|------------------|
| Containers | `docker-ce`, `python3-docker-compose` (v1, hyphenated CLI), `ca-certificates` |
| NVIDIA container runtime | `nvidia-container-toolkit` (GPU/DLA/NVENC/NVDEC/ISP in containers) — **unproven on kirkstone, see Open risks below** |
| Jetson host bits | `tegra-argus-daemon` (CSI cameras), `tegra-nvpmodel`, `tegra-nvfancontrol`, `tegra-tools` (`jetson_clocks`/`tegrastats`), `python3-jetson-stats` |
| Connectivity | `networkmanager`+`modemmanager`, `avahi`, `bluez5`, `hostapd`+`dnsmasq`, `wireguard-tools`, `chrony` |
| HMI | `packagegroup-core-x11-xserver` (Xorg + NVIDIA's Tegra X driver), `packagegroup-xfce-base` (the XFCE desktop), `xinit`/`xauth`/`xrandr`/`xset`, `boat-hmi-autostart` (autologin + `startx` → `xfce4-session` on tty1) |
| Reliability | `watchdog` |
| Security | `openssh`, `nftables` |
| Network diagnostics | `iproute2`, `net-tools`, `iputils`, `tcpdump`, `mtr`, `nmap`, `libqmi`/`libmbim`, ... |
| Field tools | `nvme-cli`, `i2c-tools`, `htop`, `tmux`, `git`, `iperf3`, ... |

Full package-by-package breakdown (and the notes on what's unavailable on
this project's kirkstone snapshot — `fail2ban`, `wavemon`, `bind-utils`,
`fake-hwclock`, `rauc`) is in
[`recipes-core/packagegroups/packagegroup-boat.bb`](recipes-core/packagegroups/packagegroup-boat.bb).

## Recipes

- `recipes-core/images/boat-image.bb` — the product image: extends
  `core-image-base`, pulls in `packagegroup-boat` + the local `boat-*`
  recipes below, and creates the fixed `boat` (UID 2000) login user via
  `extrausers`. The `DISTRO_FEATURES` it needs
  (`virtualization opengl pam x11`) are set globally by
  `scripts/02-configure-build.sh`, not here — see the note in the recipe.
- `recipes-core/packagegroups/packagegroup-boat.bb` — grouped by concern
  (`-containers`, `-nvidia-container`, `-jetson`, `-hmi`, ...) so an image
  can pull just what it needs.
- `recipes-boat/docker-config/` — `boat-docker-config`: `/etc/docker/daemon.json`
  with `default-runtime: nvidia` and `data-root` on `/data/docker`.
- `recipes-boat/hmi-autostart/` — `boat-hmi-autostart`: autologin `boat` on
  tty1, then `startx` → `boat-xfce-session` → `xfce4-session` from that login
  shell, plus the `XkbLayout` drop-in for `/etc/X11/xorg.conf.d`. Deliberately
  not poky's `xserver-nodm-init`, which would run Xorg (and therefore the
  whole desktop) as root and leave the session without a logind seat — see
  docs/05 "HMI / XFCE autostart".
- `recipes-boat/compose/` — `boat-compose`: ships Signal K / DeepStream /
  Firefox example compose files under `/usr/share/boat/compose-examples/`
  (read-only reference) and `boat-compose.service`, which runs the
  operator's own compose stack from `/data/compose` — hand-managed,
  git-versioned config-as-code, not part of the image build.
- `recipes-kernel/linux/linux-tegra_5.10.bbappend` — kernel config fragment
  for Docker (namespaces/cgroups/overlayfs/bridge/netfilter) and local
  device passthrough (`I2C_CHARDEV`, `SPI_SPIDEV`, USB-serial).

## New layers this design needs

`scripts/01-fetch-layers.sh` / `kas/xavier-nx-nvme.yml` now also fetch:

- **meta-virtualization** (`docker-ce`, `python3-docker-compose`) — also
  unlocks meta-tegra's `external/virtualization-layer` overlay
  (`nvidia-container-toolkit`, `libnvidia-container-*`), which only builds
  once meta-virtualization's `virtualization-layer` collection is present.
- **meta-tegra-community** (`python3-jetson-stats` / `jtop`).

`scripts/02-configure-build.sh` additionally enables three sublayers of the
existing `meta-openembedded` clone: **meta-xfce** for the helm desktop, plus
**meta-gnome** and **meta-multimedia**, which meta-xfce declares as
`LAYERDEPENDS`. No extra clone is needed for them.

## Deploying an app: Signal K

```
mkdir -p /data/compose
cp /usr/share/boat/compose-examples/signalk.yml.example /data/compose/docker-compose.yml
# edit: pin the image digest, point /dev/ttyUSB0 at your GNSS/NMEA adapter
systemctl start boat-compose        # or: docker-compose -f /data/compose/docker-compose.yml up -d
```

Put `/data/compose` under git for versioned, pull-to-update config (docs/05
"Docker host setup").

## Open risks — prototype before relying on them

Carried over from docs/05, unchanged by writing these recipes — none of this
has been built or flashed yet:

1. `nvidia-container-toolkit` on meta-tegra kirkstone — validate a
   `docker run --runtime nvidia ...` GPU test before trusting DeepStream.
2. DLA/ISP exposure into containers.
3. CSI camera via the `nvargus-daemon` socket across the container boundary.
4. Unprivileged Xorg (started as `boat`, relying on `systemd-logind` for DRM
   master and input) with NVIDIA's Tegra X driver — L4T's own images run X as
   root under a display manager. Not booted yet.
5. Image size / partition growth once Docker's `data-root` actually lives on
   `/data` — that partition isn't provisioned by any recipe here yet.

## Extending

`RAUC` (A/B updates), a real `/data` partition + `fake-hwclock`, and
`docs/05`'s interactive build-time user/SSH-key provisioning are deliberately
not implemented yet — they're later hardening steps, not this pass. Keep each
new concern in its own `recipes-*` directory and wire package names through
`packagegroup-boat.bb`.
