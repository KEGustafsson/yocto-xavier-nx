# meta-boat

A small Yocto layer that turns the base Xavier NX image into a **marine /
boat embedded computer**. It is Phase 2 of this project — Phase 1 gets a
plain image booting from NVMe first.

## What it adds

| Area | Packages / units |
|------|------------------|
| Navigation / GNSS | `gpsd` + clients, `chrony` (GPS/PPS-disciplined time) |
| NMEA 2000 / CAN | `can-utils`, `boat-can-setup` (brings `can0` up at 250 kbit/s) |
| Connectivity | `mosquitto` (MQTT), `avahi` (mDNS), `hostapd`+`dnsmasq` (Wi-Fi AP), `socat` |
| App runtime | `nodejs` + `npm`, `python3` — for a Signal K server and helpers |
| Reliability | `watchdog` for an unattended, power-cycled system |
| Service tools | `openssh`, `nvme-cli`, `i2c-tools`, `minicom`, `htop`, `tmux`, `rsync` |

## Recipes

- `recipes-core/images/boat-image.bb` — the product image (extends
  `core-image-base`, pulls in the packagegroup + CAN modules).
- `recipes-core/packagegroups/packagegroup-boat.bb` — grouped, so you can pull
  just `packagegroup-boat-canbus`, `-connectivity`, etc.
- `recipes-boat/can-setup/` — `boat-can0.service` configures the NMEA 2000 bus;
  edit `/etc/default/boat-can0` on target to change interface or bitrate.

## Signal K

[Signal K](https://signalk.org) is the de-facto open marine data server and is
a Node.js application. Two ways to ship it:

1. **Runtime install (quickest):** the image already contains `nodejs`/`npm`.
   On the target: `npm install -g signalk-server` then enable it as a systemd
   service. Good for prototyping.
2. **Baked in (production):** add a `signalk-server_git.bb` recipe using the
   `npm` / `npmsw` fetchers so the whole dependency tree is fetched at build
   time and shipped read-only. This is more work but reproducible and offline.

Start with option 1, then graduate to option 2 once your feature set is stable.

## Extending

Add navigation UIs (OpenCPN needs a graphics stack — build on `demo-image-egl`
or add Weston), AIS decoders, autopilot bridges, InfluxDB/Grafana logging, a
read-only root overlay for power-loss resilience, etc. Keep each concern in its
own `recipes-*` directory and wire it through the packagegroup.
