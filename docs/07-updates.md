# 07 — OS updates: A/B slots, over the air

> **Experimental.** The A/B layout is off by default (`AB_LAYOUT=0`) until the
> bench validation in [the plan](plans/ab-os-updates.md) (Phase 7) has passed
> on hardware. Everything below describes `AB_LAYOUT=1` builds.

The A/B layout keeps **two complete root filesystems** ("slots") on the SSD.
An update is written into the slot that is *not* running, and the board then
tries it **once**. If the new slot does not come up healthy, the board goes
back to the slot it came from by itself, with nobody at the console. Signed
update bundles reach the board over the air, from a plain HTTPS server.

What it does **not** do:

- **Install or reboot on its own.** A check and a background download can
  be automatic; writing the other slot, and the reboot into it, always takes
  `boat-update install`, typed by a person.
- **Update firmware.** QSPI firmware, the kernel DTB and anything else that
  changes with a new L4T release still need a USB reflash at the dock. A
  bundle built on another L4T release is refused.

The design, and why it is shaped this way, is in
[`plans/ab-os-updates.md`](plans/ab-os-updates.md).

## How a boot picks its slot

```text
UEFI -> L4TLauncher -> GRUB (NVMe ESP) -> kernel + initrd of slot A or B
                         |
                         grubenv: ORDER, A_OK/A_TRY/A_COMMITTED, B_...
```

- GRUB boots the first usable slot in `ORDER`, and **counts the attempt
  before the kernel loads**.
- A committed slot gets two attempts, so one power cut during boot never
  moves the boat to an older OS. A slot **on trial** — a fresh update —
  gets one.
- **`boat-commit`** decides whether a boot worked. On a trial it checks
  that the critical services are up
  (`/etc/boat-update/health.conf`). If they are, the slot is committed. If
  not, it is marked bad and the board reboots into the other slot.
- Every other way of failing also ends in a reset:
  - a kernel panic;
  - an initramfs that cannot find its root;
  - systemd's emergency mode;
  - a wedged PID 1 (the hardware watchdog);
  - a trial still not committed after 15 minutes;
  - a GRUB that hangs (the UEFI watchdog), or a `grub.cfg` that is missing
    or cut short.

  The reset is what lets GRUB count the attempt and fall back.
- If **no slot qualifies** any more, GRUB still boots one, as a **last
  resort**, without counting: a slot that once passed its trial first, the
  flashed slot A otherwise. `boat-update status` then shows an ALERT. A
  committed slot is made usable again once it boots; a slot that never
  passed a trial is not - install a release, or reflash.

## Turning it on

### 1. Make the signing keys — once, ever

```bash
scripts/gen-update-keys.sh            # into ~/.local/share/boat-update-keys
```

This makes an update CA and two signing keys under it:

- one for bundles;
- one for the channel index.

Every image trusts exactly that CA.

> **Back the directory up now, somewhere other than this machine.** Without
> `ca.key.pem` and `ca.cert.pem`, no board will trust a new bundle until it
> has been reflashed over USB. Once the backup exists, keep `ca.key.pem`
> offline: it is only needed to issue new signing certificates.

`BOAT_KEYS_DIR` points elsewhere if you keep the keys on a removable drive.
The build refuses a key directory inside the repository or the work tree,
and `scripts/lint.sh` fails if a private key is ever committed.

### 2. Build

```bash
export AB_LAYOUT=1
export BOAT_RELEASE=1.0.0            # what /etc/boat-release and the bundle say
scripts/01-fetch-layers.sh           # also fetches meta-rauc
scripts/02-configure-build.sh
IMAGE=boat-image scripts/03-build.sh
scripts/03-build.sh boat-bundle      # the update bundle (and the image in it)
```

With kas: `AB_LAYOUT=1 BOAT_RELEASE=1.0.0 scripts/build-kas.sh` builds the
image, and `AB_LAYOUT=1 BOAT_RELEASE=1.0.0 scripts/build-kas.sh -- --target
boat-bundle` the bundle. The build checks the release and the key directory
itself either way.

**`BOAT_RELEASE` must grow with every release you ship.** It is compared as
a version: `1.0.0 < 1.0.1 < 1.1.0-rc.1 < 1.1.0`, and `2026.10.1` works too.
Left empty, a build reports `0.0.0-dev`. That is fine on the bench, but such
a build is never offered over the air.

**After a security fix, raise the floor.** `BOAT_MIN_BUNDLE_VERSION` (in
`scripts/env.sh`, or the environment for kas) is the oldest release a board
will install, over the air, by `push-update.sh` or from a USB stick alike.
Set it to the release with the fix, in that release's build: from then on a
board running it refuses anything older. Empty means no floor; it can never
be above `BOAT_RELEASE` (the build stops).

**A slot holds about 12 GiB of image content.** Each slot is 16 GiB; the
build adds 30 % and 512 MiB to the content and stops if the result no longer
fits.

### 3. Flash — this erases the SSD

The A/B layout is a different partition table. The first flash after
switching `AB_LAYOUT` on (or off) is a full `scripts/05-flash-nvme.sh` in
recovery mode, and **it erases everything on the SSD**, `/data` included.
Copy off what you need first. Then, with the board in recovery mode:

```bash
scripts/04-unpack-tegraflash.sh boat-image   # the image you just built
scripts/05-flash-nvme.sh
```

Unpack first: `05-flash-nvme.sh` flashes whatever was unpacked last. It
refuses an unpack whose layout does not match `AB_LAYOUT`, so an older
single-slot unpack is not written back over the erased SSD.

Use the default `initrd-flash` path. `--host-drive` relies on a
`doexternal.sh` that meta-tegra's kirkstone branch does not ship, and the DATA
partition's first filesystem is written by `initrd-flash`.

After the first boot, slot A runs, slot B is not used, and `/data` fills
the rest of the SSD (`boat-storage status`). (A flash does not write slot
B: on a board that already had the A/B layout, the old slot B is still
there, marked unusable, until the first update overwrites it.)

## Day to day: `boat-update`

```bash
boat-update                  # status: slots, versions, last result, OTA state
sudo boat-update check       # ask the channel now
sudo boat-update download    # fetch the offered bundle now (resumable)
sudo boat-update install --reboot     # write it to the other slot, reboot into it
sudo boat-update install --stream --reboot   # same, streamed from the channel
boat-update history          # installs, trials and what became of them
sudo boat-update rollback    # switch to the other slot (if it is usable) and reboot
sudo boat-update debug-hold on|off    # next boot: stop for inspection after a failure, not reset
```

`boat-update` exits 0 when it did what was asked, and 1 when it refused or
failed (the message says why). `download` exits 2 when it ended without a
ready bundle - waiting for an unmetered link, no progress, or nothing on
offer - so `download && install --reboot` stops there. A command you type
does not wait on the background check or download: it stops it (the
download carries on from where it was at its next run) and goes ahead.
`rollback` sets GRUB's state itself, so it works even when `/data`, and so
RAUC, is what the new release broke.

`install` runs as its own systemd unit, so losing the SSH session half way
does not stop it; it still exits non-zero if the install fails. After the reboot the new slot is **on trial**. `boat-update
status` says so, and sleep is held off until `boat-commit` has decided.
That takes a minute or two. `boat-sleep` refuses meanwhile, and while an
install runs, even with `--force`.

Turning the computer off, or rebooting it, before the verdict does not fail
the release: the next boot tries it again. That happens **once** per
install - a second shutdown before the verdict counts as a failed trial, as
a power cut always does.

Take a USB stick out before `--reboot`. The firmware is set to boot the SSD
first, but a board flashed before that setting may still boot a bootable
stick instead, and then neither slot runs.

- **It committed:** the old slot stays as the rollback. The downloaded
  bundle is deleted, because the old slot, not the file, is what you go back
  to.
- **It failed:** the board is back on the old slot.
  - `boat-update history` and `/data/boat-update/last-result` say why:
    the health check that failed, or - when the trial ended before
    `boat-commit` could decide (a kernel panic, a hang, a power cut, a
    slot GRUB could not load) - that it ended without a verdict.
  - `journalctl -b -1` shows the failed boot itself; the journal lives on
    `/data`.
  - That version is never offered to this board again - also when the
    trial ended before `boat-commit` could say so (a kernel panic, a power
    cut): `boat-update` notices the uncommitted slot it installed into.

**Without internet**, from a laptop on the boat's network:

```bash
scripts/push-update.sh yocto/build/tmp/deploy/images/jetson-xavier-nx-devkit/boat-bundle-jetson-xavier-nx-devkit.raucb
```

That file is the link to the last bundle built; run it from the
repository root. (A `boat-bundle-*` pattern names both the bundle and that
link.) A second argument is the board, as `user@host`; without one,
`wol/boat.conf` names it. It copies the bundle to the board, installs it,
reboots, waits, and tells you which slot came back. From a USB stick, use exFAT or ext4 (a bundle over
4 GiB does not fit on FAT32), then `sudo boat-update install /media/<stick>/<bundle>.raucb --reboot`.

## Over the air: the update channel

A channel is **static files on any HTTPS server that supports Range
requests**: nginx, Caddy, or object storage. Range support is what lets a
download resume after the link drops. Check a host before you rely on it:

```bash
scripts/ab-phase0-capture.sh --channel-url https://updates.example/boat-xavier-nx/stable/index.p7
```

That check runs from the build host, after a read-only probe of the board
over SSH, and with the host's own CA store. What counts is the board: once
the channel is set, `sudo boat-update check` on the board shows whether it
can reach the channel and trusts its certificate (a server with a CA of its
own needs `server-ca.pem`, below).

GitHub Releases is a poor fit: bundles that carry CUDA can exceed its per-file
size limit.

```text
https://updates.example/boat-xavier-nx/
    stable/index.p7                 the signed index the boats follow
    stable/boat-bundle-<version>-<sha256[:12]>.raucb
    testing/...                     a test board running a boat image: each release goes here first
    bench/...                       the A/B validation bench (BOAT_BENCH images and bundles only)
```

`testing` and `bench` are not the same thing. A **bench** image
(`BOAT_BENCH=1`, [the validation runbook](plans/ab-validation.md)) trusts the
bench CA, has its own RAUC `compatible`, and follows `bench`: it validates
the A/B design, with fault bundles, and takes nothing built for a boat. A
release is tried on a board that runs an ordinary boat image and follows
`testing`; the same file then goes to `stable`.

### Publishing

```bash
BOAT_PUBLISH_DEST=user@updates.example:/srv/www/boat-xavier-nx \
  scripts/publish-update.sh testing yocto/build/tmp/deploy/images/jetson-xavier-nx-devkit/boat-bundle-jetson-xavier-nx-devkit.raucb
```

The script:

1. reads the version and L4T release from the bundle itself;
2. names it by its SHA-256;
3. writes and signs the index with the index key and the next serial (at
   least the time in seconds, so a restored key directory or a second
   publishing machine never goes backwards);
4. uploads the bundle **first** and the index **last**.

To promote a release once it has committed on the `testing` board, publish
the same bundle file to `stable` - not a rebuild, which would be a
different file nobody has tried. Keep a copy of each bundle you publish:
the next build of `boat-bundle` deletes it from the deploy directory.
`--reflash` marks a BSP bump: boards are told to reflash at the dock
instead of downloading it. `--notes FILE` attaches release notes.

### What the board accepts

`boat-update check` fetches `index.p7` (https only, no redirects, at most
64 KiB, enforced as it arrives). It verifies it against the update CA in
`/etc/rauc`, **never** against the system's public CAs, and only from the
index-signing certificate (the emailProtection key usage). It then accepts
it only if all of these hold:

- the index is for this channel;
- its serial is above the last one accepted on this channel — so an old
  index cannot be replayed;
- it is for this board and this L4T release;
- it offers a version that is newer than the running one, not already in
  the other slot, and not one that failed a trial here.

RAUC checks the bundle's own signature again before it is installed. The
channel host is not trusted for anything but availability: the worst it can
do is withhold updates. A download never grows past the size the signed
index gives. A bundle that will not download intact - three re-fetches - or
that RAUC refuses is **given up**, not counted as a failed trial: a new
index (the next publish) tries it again. `status` shows when the channel
last published.

A check or download that fails shows in `status` as an **ALERT**, with its
reason: an index that does not verify or is a replay, a channel whose
certificate the board does not trust, a download `/data` has no room for.
It stays until a check works again (a failed download: until a download
finishes, or the offer goes away). A refusal that is only the board's
state is not an alert, and the timer's run does not count it as failed: on
trial, no network or a channel nothing answers for, the clock not set yet,
another `boat-update` running.

### On the board: `/data/boat-update/channel.conf`

The image ships defaults in `/etc/boat-update/channel.conf`. Put your
settings in **`/data/boat-update/channel.conf`**. That file is shared by both
slots, so it stays right across updates and rollbacks.

```bash
BOAT_UPDATE_URL="https://updates.example/boat-xavier-nx"   # the final URL - redirects are not followed
BOAT_UPDATE_CHANNEL=stable          # testing on the test board, bench on a bench image
BOAT_UPDATE_AUTO=download           # off | check | download - never installs
BOAT_UPDATE_METERED=no              # download over a metered link?
BOAT_UPDATE_RATE_LIMIT=""           # e.g. 200k
```

- **Schedule.** The timer looks for updates four times a day, after every
  network change, and after waking from sleep. A check made before chrony
  has set the clock (the board has no battery-backed clock) runs again a
  few times within the hour.
- **Downloads.** They run in ten-minute windows. Each one resumes where the
  last stopped, whether that was a dropped link, a sleep or a reboot.
- **Disk space.** A download needs the bundle's size plus 2 GiB free on
  `/data`.

**Metered links.** A download waits while the link the boat is using is
metered: cellular, or anything NetworkManager guesses is. **A 4G router
reached over Wi-Fi or Ethernet looks unmetered.** Mark that connection:

```bash
nmcli connection modify "<the router's connection>" connection.metered yes
```

With a VPN as the default route, the board counts as metered if any of its
links is. `status` shows "waiting for an unmetered link", and `sudo
boat-update download --allow-metered` overrides it once.

**Access control.** A public channel is the default. Bundles hold nothing
secret: the image's password hash is already in this repository. To restrict
it, use a TLS client certificate:

```bash
sudo install -m 644 client.crt /data/boat-update/client.crt
sudo install -m 600 client.key /data/boat-update/client.key   # root's alone - checked
```

`boat-update` refuses a key anyone but root can read. Basic auth and bearer
tokens are not supported, because they leak across redirects and into logs.

**The channel's own certificate.** The board checks the channel's TLS
certificate against the image's `ca-certificates`. A server whose
certificate comes from a CA of its own - a bench server, or Caddy's
`tls internal` for a LAN address - needs that CA on the board:

```bash
sudo install -m 644 server-ca.pem /data/boat-update/server-ca.pem
```

or `BOAT_UPDATE_SERVER_CA=<file>` in `channel.conf`. The board then trusts
that CA, and only that CA, for the channel: downloads, checks and streams.
Keep it on `/data`: a CA added to the running slot's `/etc/ssl` is gone
after the next update, because the other slot is a fresh image.

### Streaming instead of downloading

```bash
sudo boat-update check
sudo boat-update install --stream --reboot
```

`--stream` has RAUC read the offered bundle straight from the channel into
the other slot. Every bundle is built **adaptive**, so RAUC fetches only
the blocks that neither slot already has. On a small change that is a
fraction of the bundle; RAUC's own documentation suggests about a tenth.
RAUC never talks to the channel itself: it streams from a relay inside
`boat-update` on `127.0.0.1`, which fetches each block from the verified
URL the way `boat-update`'s own downloads do - `https` only, no redirect
ever followed, with the client certificate (which RAUC never sees, so the
key stays root-only), and only over the link that was checked when the
stream started. That link must not be metered unless `--allow-metered` is
given (or `BOAT_UPDATE_METERED=yes`); either way the stream stays bound to
it. The relay serves RAUC's streaming client alone - no other user or
container on the board - and passes on only an answer for exactly the
range RAUC asked for. If that link goes away mid-stream, the stream fails
and the board stays on its slot; it never moves to another route. With a
VPN as the default route that is not so - the tunnel follows whatever link
carries it, metered or not - so a stream over a VPN needs
`--allow-metered`; download, then install, instead. On top of that:

- before RAUC starts, `boat-update` asks for the bundle's first byte and
  refuses to stream from a channel that redirects it or ignores Range
  requests;
- the bundle must be the release the index offered (RAUC itself checks
  only its signature): RAUC's pre-install handler refuses any other before
  anything is written, and the slot is checked again after the stream,
  and marked bad before anything boots it if it is not.

A stream is offered only what a download would be: newer than the running
release, not a release that failed a trial here, and checked on the channel
that is configured now (after changing channels, `check` again).

| | download, then install | `install --stream` |
|---|---|---|
| link | any - resumes across drops, sleeps and reboots | must stay up for the whole install |
| bytes over the link | the whole bundle | the changed blocks |
| a drop half way | the next window resumes | RAUC aborts, the board stays on its slot, run it again (a retry reuses what it already has) |
| use it | on shore Wi-Fi, cellular, anything flaky | at anchor on a decent link |

Measured transfers on this board are filled in by the bench validation
(plan Phase 6):

| change | bundle | streamed |
|---|---|---|
| one package | not yet measured | not yet measured |
| kernel | not yet measured | not yet measured |
| CUDA | not yet measured | not yet measured |

To measure: note the default-route interface's RX bytes
(`/sys/class/net/<dev>/statistics/rx_bytes`) before and after an `install
--stream`.

Streaming needs the channel's Range support (the same check as above) and
kernel NBD support, which the A/B image builds in. A server without Range
support is refused before RAUC starts, with a message saying so; download-
then-install is the fallback.

## Storage on the A/B layout

| partition | what it holds |
|---|---|
| `APP`, `APP_b` | slot A and slot B, 16 GiB each (`ROOTFS_SIZE_BYTES`) |
| `SWAP` | 8 GiB of swap |
| `DATA` | everything else, on `/data`, shared by both slots |

`/data` holds:

- Docker (`/data/docker`) and the compose files;
- `/home/boat`;
- the persistent journal (capped at 256 MiB);
- RAUC's state and `boat-update`'s state.

`root`'s home stays in the slot, so its SSH keys still work if DATA is what
broke.

Settings that belong to the board rather than the image are copied into the
new slot at every install:

- host keys (`/etc/ssh/ssh_host_*`) and `/etc/machine-id`;
- `root`'s SSH keys (`/home/root/.ssh`);
- the Wi-Fi and network connections (`/etc/NetworkManager/system-connections`);
- Bluetooth pairings (`/var/lib/bluetooth`);
- hostname and time zone;
- the `root` and `boat` passwords;
- the sleep listener's key (`/etc/boat-sleep.key`) and its port, if you
  moved it (`systemctl edit boat-sleep-listener.socket`).

The configuration files you are meant to edit are carried **only if you
changed them**: `/etc/default/boat-power`, `/etc/default/boat-bluetooth`,
`/etc/default/boat-sleep-listener`, `/etc/bluetooth/main.conf`,
`/etc/NetworkManager/conf.d/90-boat-wol.conf`, `/etc/docker/daemon.json` and
`/etc/boat-update/health.conf`. One you never touched takes the new
release's version, so a release can change its defaults; one you deleted
stays deleted. (An image built before this rule carries them all, once.)

**Any other edit under `/etc` does not survive an update.** Put it in
`meta-boat`, or on `/data`.

`boat-storage status` shows the partitions. If `/data` will not mount, the
boat still boots without it — the shared mounts are all optional — and
`journalctl -b -u boat-data-check` says why. `sudo boat-storage format-data`
makes a new, empty DATA filesystem. It asks first, and only you can run it:
nothing formats DATA on its own.

`boat-grow-rootfs` is not part of an A/B image; the layout already uses the
whole SSD.

## When something goes wrong

- **"booted OUTSIDE GRUB"** in `status`. GRUB did not run, and L4TLauncher's
  own fallback booted slot A. Usually the UEFI setting was reset to its
  default. `boat-bootmode-guard` puts it back, so reboot once. Updates are
  refused until the board comes up through GRUB.
- **"grubenv was torn and rebuilt."** A power cut hit a write of the boot
  state. The running slot was kept. The other slot is marked unusable until
  the next update, unless this boot was a trial — then the slot you updated
  from stays the fallback.
- **It keeps falling back after an update.** Read `boat-update history`
  and `journalctl -b -1 -u boat-commit`.
  - A health check that fails for reasons outside the OS — an app that
    needs the internet, say — belongs out of
    `BOAT_HEALTH_UNITS`/`health.d`.
  - On the bench, `sudo boat-update debug-hold on` keeps a failing slot up
    for inspection. It lasts until a boot that carried it comes up
    healthy, then turns itself off; `debug-hold off` ends it sooner.
- **"GRUB found no usable slot: this is its last resort boot."** Both
  slots used up their attempts. The ALERT says whether the running slot was
  made usable again (it had passed a trial) or not (a failed release, or an
  install that was cut off - install a release, or reflash).
- **"could not reset slot X's attempt count"**, or **"boat-grubenv-guard
  failed"**. The ESP will not take writes - usually a damaged filesystem.
  While this lasts every reboot counts against the slot, and two of them
  hand the board to the other slot. Reflash at the dock.
- **The board stops in the UEFI menu, or will not boot at all.** Both
  slots failed repeatedly, and NVIDIA's own boot counter has marked the
  drive unbootable. That marker is sticky. Recover with a USB reflash
  (`scripts/04-unpack-tegraflash.sh boat-image`, then
  `scripts/05-flash-nvme.sh`), which keeps nothing on the SSD: `/data` starts
  empty. That is the only recovery known today. The UEFI menu may be able
  to clear the marker without a reflash, but where is not known yet: the
  Phase 0 runbook (2.6) is where it gets found, and then it is written here
  and in [`06-troubleshooting.md`](06-troubleshooting.md).
- **"BSP bump: reflash at the dock."** The new release is built on another
  L4T. Flash it over USB. Like the first flash, **a USB flash empties
  `/data`**: copy off what you need first.

See also [`06-troubleshooting.md`](06-troubleshooting.md).
