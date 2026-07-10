# 05 — Phase 2: The Boat Computer Layer (`meta-boat`)

With a plain image booting from NVMe, add the marine software via the
`meta-boat` layer (already in `bblayers.conf`). Now build the product image
instead of `core-image-base`:

```bash
IMAGE=boat-image ./scripts/03-build.sh
./scripts/04-unpack-tegraflash.sh
./scripts/05-flash-nvme.sh --external-only     # firmware already flashed in Phase 1
```

Nothing else changes — same NVMe boot path, same flashing flow.

## What you get

`boat-image` = `core-image-base` + `packagegroup-boat` + CAN setup + CAN kernel
modules. Grouped so you can trim it:

| Sub-group | Contents |
|-----------|----------|
| `packagegroup-boat-nav` | `gpsd` (+clients), `chrony` |
| `packagegroup-boat-canbus` | `can-utils`, `iproute2` |
| `packagegroup-boat-connectivity` | `mosquitto`, `avahi`, `hostapd`, `dnsmasq`, `socat` |
| `packagegroup-boat-runtime` | `nodejs`, `npm`, `python3` |
| `packagegroup-boat-reliability` | `watchdog` |
| `packagegroup-boat-tools` | `openssh`, `nvme-cli`, `i2c-tools`, `minicom`, `htop`, `tmux`, `rsync` |

See [`../layers/meta-boat/README.md`](../layers/meta-boat/README.md) for the recipe map.

## NMEA 2000 / CAN on Xavier NX

The Xavier NX SoC has an **mttcan** controller exposed as `can0`/`can1`. Wiring
it to a physical NMEA 2000 backbone needs an external **CAN transceiver**
(e.g. SN65HVD230, 3.3 V) on the 40-pin header's CAN pins, plus proper 120 Ω
bus termination.

`boat-can-setup` installs a systemd unit that brings `can0` up at the NMEA 2000
bitrate (250 kbit/s) on every boot:

```bash
systemctl status boat-can0.service
candump can0            # watch NMEA 2000 traffic
cansend can0 1DEFFF00#0102030405060708
```

Change interface/bitrate in `/etc/default/boat-can0`, then
`systemctl restart boat-can0.service`. Decode NMEA 2000 PGNs with a userspace
tool such as [`canboat`](https://github.com/canboat/canboat) (add a recipe) or
let Signal K's `canboatjs` handle it.

## GNSS + time

`gpsd` reads a USB or UART GNSS receiver; point it at the device in
`/etc/default/gpsd`. Discipline the clock from GPS (and PPS, if wired) with
`chrony` so logs and NMEA timestamps are correct without an RTC/network.

## Signal K server

[Signal K](https://signalk.org) unifies NMEA 0183/2000, GPS and sensor data and
serves it over HTTP/WebSocket to plotters, phones and apps. It is a Node.js app;
the image already ships `nodejs`/`npm`.

**Prototype (fast):**

```bash
npm install -g signalk-server
signalk-server-setup            # generates config + a systemd unit
```

**Production (reproducible/offline):** write a `signalk-server_git.bb` recipe in
`meta-boat/recipes-boat/signalk/` using the Yocto `npm`/`npmsw` fetchers so the
dependency tree is fetched at build time and shipped read-only, with a systemd
service that starts it at boot. See the meta-boat README for the two-step path.

## Wi-Fi access point (helm hotspot)

`hostapd` + `dnsmasq` are included to turn the Xavier NX into an onboard Wi-Fi AP
so tablets/phones reach Signal K without shore internet. Add `hostapd.conf`
(SSID/passphrase/channel) and a `dnsmasq` DHCP range via a bbappend or a small
config recipe, and enable both services.

## Reliability for a boat

An always-on system that loses power without a clean shutdown wants:

- **`watchdog`** (included) tied to the Tegra hardware watchdog so a hung system
  auto-reboots.
- A **read-only root + writable overlay** (`overlayfs`) or a data partition for
  logs, so an abrupt power cut can't corrupt the rootfs. Add via a distro/image
  feature — a good next hardening step.
- Ship a **BUP** (bootloader update payload) path for field firmware updates
  (meta-tegra `generate_bup_payload.sh`) so you don't need recovery-mode access
  on a boat.

## Adding more

Keep each concern in its own `recipes-*` directory and wire package names into
`packagegroup-boat.bb`. Candidates: AIS decoding, autopilot/NMEA 0183 bridges
(`socat`/`kplex`), InfluxDB + Grafana logging, OpenCPN charting (needs a Wayland/
Weston graphics image — build on `demo-image-egl`/`-weston` rather than
`core-image-base`), and MQTT bridging to shore.

Next: [`06-troubleshooting.md`](06-troubleshooting.md)
