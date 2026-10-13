# A/B validation: running the acceptance matrix

Companion to [the A/B update plan](ab-os-updates.md), section 7 and Phase 7.
`AB_LAYOUT` stays off by default until every row below has passed on a
**bench board**; then the boat moves to the `stable` channel. Record each
result (date, image release, pass/fail, notes) at the bottom of this file in
the PR that flips the default.

## What already runs on every PR

`scripts/lint.sh` (CI runs it with `LINT_STRICT=1`) covers the logic that
can run off-target:

| harness | what it proves | matrix rows it covers off-target |
|---|---|---|
| `scripts/test-grub-ab.py` | the shipped `grub.cfg` in real GRUB (`grub-emu`): attempt counting, trial vs committed limits, torn/missing `grubenv`, an ESP that refuses writes, a failed kernel load | 10, 11, 12, 21 (GRUB side) |
| `scripts/test-ab-scripts.py` | `boat-grubenv-guard`, `boat-commit` and RAUC's pre/post-install handlers against a fake board | 9, 13-16 and 18 (decision side), 20, 21, 50 |
| `scripts/test-boat-update.py` | `boat-update` against a real local HTTPS channel: signatures, replay, channel, metered link, interrupted and corrupted downloads, streaming through the relay (a later redirect, wrong or short ranges, the bound link) | 31-36, 38, 42, 43, 46, 47 (logic) |

None of that replaces the bench: firmware, the watchdogs, power cuts and RAUC
itself only behave like the real thing on the real thing.

## Bench setup

1. **Keys.** Run `scripts/gen-update-keys.sh` for the fleet keys if they do
   not exist yet. Then run `scripts/gen-update-keys.sh --bench` for a
   **separate** bench CA, in `~/.local/share/boat-update-bench-keys`.
2. **A bench image.** It trusts both CAs, so it accepts fault bundles.
   A `BOAT_BENCH` build also gets its own RAUC `compatible`
   (`boat-xavier-nx-bench`), so nothing it builds - not even a good bundle,
   whose rootfs trusts the bench CA - installs on a boat, and
   `publish-update.sh` sends it to the `bench` channel only. A bench board
   goes back to boat images by reflashing. The commands below run in a
   subshell, so `BOAT_BENCH` does not stay set for a later boat build:

   ```bash
   (
     export AB_LAYOUT=1 BOAT_BENCH=1 BOAT_RELEASE=1.0.0 IMAGE=boat-image
     scripts/02-configure-build.sh && scripts/03-build.sh
     scripts/04-unpack-tegraflash.sh
     scripts/05-flash-nvme.sh            # erases the SSD
   )
   ```

3. **Bundles.** You need a good bundle, and one fault bundle per kind. Each
   build replaces the previous one in the deploy directory, so copy each
   out as it is built:

   ```bash
   (
     export AB_LAYOUT=1 BOAT_BENCH=1
     source scripts/env.sh                # BUILD_DIR, MACHINE
     deploy="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
     mkdir -p ~/bench
     BOAT_RELEASE=1.0.1 scripts/02-configure-build.sh && scripts/03-build.sh boat-bundle
     cp -L "$deploy/boat-bundle-${MACHINE}.raucb" ~/bench/good.raucb
     for f in panic emergency tier2 hang; do
       BOAT_FAULT=$f scripts/02-configure-build.sh && scripts/03-build.sh boat-bundle-faulty
       cp -L "$deploy/boat-bundle-faulty-$f-${MACHINE}.raucb" ~/bench/faulty-$f.raucb
     done
   )
   ```

   A fault bundle installs normally, then fails its **trial** boot in one
   way:

   | `BOAT_FAULT` | what the trial boot does | row |
   |---|---|---|
   | `panic` | kernel panic | 13 |
   | `emergency` | systemd emergency mode | 15 |
   | `tier2` | NetworkManager masked, so the health gate fails | 16 |
   | `hang` | `boat-commit` never finishes | 18 |

4. **A channel** for the OTA rows. Use any local HTTPS server with Range
   support, such as Caddy, serving the `bench` channel. Check it with
   `scripts/ab-phase0-capture.sh --channel-url …`, publish to it with
   `scripts/publish-update.sh bench <bundle>`, and set
   `BOAT_UPDATE_CHANNEL=bench` in the board's
   `/data/boat-update/channel.conf`.
5. **Optional: a USB relay on the barrel jack** for the power-cut rows and
   the soak. Its on/off command goes in `BOAT_SOAK_POWER_CUT_CMD`.

## The soak

`scripts/ab-soak.sh` alternates good and faulty installs, and refuses to
start on a board that is not running a bench image. Every cycle must
really reboot the board (a new boot ID). Each good bundle must come back on
the other slot and commit; each faulty one must come back on the slot it
started from, with the other slot marked bad - so a bundle that was
refused before anything was written fails the cycle. A power-cut cycle
waits for the cut to have happened, then only has to end committed on
either slot; a cut command that fails fails the cycle. The results go to a
CSV with a `delivery` column, and the exit status is the number of failed
cycles. It runs two ways; record each run as its own result.

**Over SSH** - quick, and needs no new versions: the same bundles are
copied to the board and installed again and again.

```bash
scripts/ab-soak.sh --good ~/bench/good.raucb \
  --faulty ~/bench/faulty-panic.raucb --faulty ~/bench/faulty-emergency.raucb \
  --faulty ~/bench/faulty-tier2.raucb --faulty ~/bench/faulty-hang.raucb \
  --cycles 50 [--power-cut-every 5]
```

**Over the air** - the plan's Phase 7 target: 50 cycles delivered from the
`bench` channel. A board takes only a release newer than the one it runs
(and never one that failed a trial), so every cycle needs its own bundle,
with versions growing in cycle order and above anything the board has run.
Build them once, named so that name order is cycle order:

```bash
(
  export AB_LAYOUT=1 BOAT_BENCH=1
  source scripts/env.sh
  deploy="${BUILD_DIR}/tmp/deploy/images/${MACHINE}"
  mkdir -p ~/bench/ota
  faults=(panic emergency tier2 hang)
  for i in $(seq 1 25); do
    g=$((2 * i - 1)); f=$((2 * i)); kind=${faults[$(( (i - 1) % 4 ))]}
    BOAT_RELEASE=1.1.$g scripts/02-configure-build.sh && scripts/03-build.sh boat-bundle
    cp -L "$deploy/boat-bundle-${MACHINE}.raucb" ~/bench/ota/$(printf %02d $g)-good.raucb
    BOAT_RELEASE=1.1.$f BOAT_FAULT=$kind scripts/02-configure-build.sh \
      && scripts/03-build.sh boat-bundle-faulty
    cp -L "$deploy/boat-bundle-faulty-$kind-${MACHINE}.raucb" \
      ~/bench/ota/$(printf %02d $f)-faulty-$kind.raucb
  done
)
BOAT_PUBLISH_DEST=<the bench channel root> \
  scripts/ab-soak.sh --ota ~/bench/ota [--stream] [--power-cut-every 5]
```

Cycle N publishes the Nth bundle to `bench` with `publish-update.sh`; the
board then runs `boat-update check`, `download` and `install --reboot`
(with `--stream`: `check` and `install --stream --reboot`). A name with
`-faulty` in it is a faulty bundle. A run with `--stream` is a third
result of its own.

## The matrix, row by row

**auto** means the soak or a harness above shows it; **bench** means a
manual step on the bench board. The expected results are the plan's.

| # | how | procedure |
|---|---|---|
| 1 | auto | the soak's good cycles |
| 2 | bench | `rauc install` a bundle signed by a third, unknown CA, then a fault bundle on a **non-bench** image: both rejected, `boat-update status` unchanged |
| 3 | bench | build a bundle with `RAUC_BUNDLE_FORMAT = "plain"`: rejected (`bundle-formats=-plain`) |
| 4 | bench | build with another `BOAT_BUNDLE_COMPATIBLE`: rejected |
| 5 | bench | image with `BOAT_MIN_BUNDLE_VERSION = "2.0"`, install a 1.x bundle: rejected |
| 6 | bench | edit a bundle's `[meta.boat] l4t` (rebuild with a patched recipe): the pre-install handler refuses |
| 7 | bench | `date -s 1970-01-02` (chrony stopped), install a good bundle: installs |
| 8 | bench | relay cut during `rauc install`'s write: comes back on the old slot, target `OK=0`; install again works |
| 9 | bench | cut right after "installed", before the reboot: the next boot is a **trial** of the new slot |
| 10 | bench | cut while the new slot boots: old slot, new slot `OK=0`, `status` says so |
| 11 | bench | on a committed slot, one cut during boot: stays on that slot |
| 12 | bench | from the other slot, mount the committed slot and overwrite 1 MiB in the **middle** of its `/boot/Image` (`dd if=/dev/urandom of=…/boot/Image bs=1M seek=8 count=1 conv=notrunc`), keeping the header intact so GRUB loads it and the kernel dies: A, A, then B, with no NVIDIA recovery state. A damaged header fails GRUB's `linux` command instead (row 19's path: A, then B). |
| 13 | auto | `boat-bundle-faulty-panic` |
| 14 | bench | after an install, from the running slot, damage only what the kernel reads and GRUB does not: the new slot's ext4 journal. `debugfs -R "bmap <8> 0" <new slot's partition>` gives the journal's first block; zero it with `dd if=/dev/zero of=<partition> bs=4096 seek=<block> count=1 conv=notrunc`. GRUB still loads the kernel, the root mount fails, the initramfs reboots: back on the old slot. (Overwriting the start of the partition breaks the superblock, and GRUB fails the slot first - row 19's path, not this one.) |
| 15 | auto | `boat-bundle-faulty-emergency` |
| 16 | auto | `boat-bundle-faulty-tier2`: `boat-update history` names NetworkManager |
| 17 | bench | during a trial boot, hang the kernel hard: `echo HARDLOCKUP > /sys/kernel/debug/provoke-crash/DIRECT` on a kernel with `CONFIG_LKDTM` (not in the default config - a bench kernel fragment). The watchdog resets the board, back on the old slot. Without LKDTM, record this row as not run. |
| 18 | auto | `boat-bundle-faulty-hang`: at the 15-minute deadline `boat-commit` fails the trial and reboots |
| 19 | bench | `bitbake boat-bundle` with an image lacking `/boot/Image`: the build fails |
| 20 | bench | on a committed boot, `sgdisk -c 18:XXX` (rename DATA): boots, no Docker, alert in `status`; rename back |
| 21 | bench | `dd if=/dev/zero of=/boot/efi/EFI/BOOT/grubenv bs=1k count=1`, reboot: boots, "grubenv was torn and rebuilt"; install works |
| 22 | bench | `efivar -w` `L4TDefaultBootMode` = 1, reboot: "booted outside GRUB", install refused; next boot through GRUB |
| 23 | bench | rename `grubaa64.efi`: DIRECT boots A, flagged |
| 24 | bench | a JetPack SD card in the module slot plus a USB stick labelled `APP`/`esp`: everything still resolves to the NVMe |
| 25 | bench | as `boat`: `busctl call de.pengutronix.rauc / de.pengutronix.rauc.Installer InstallBundle sa{sv} /x 0` is denied; also from a container running as a non-root user with `/run/dbus` mounted (a container running as root is root to D-Bus, plan 5.7) |
| 26 | bench | `boat-update rollback` after a commit: the other slot boots and commits |
| 27 | bench | `boat-sleep` during an install and during a trial: refused (sleep inhibitor) |
| 28 | bench | both slots' kernels corrupted: A, A, B, then UEFI stop; the documented recovery (docs/07, Phase 0 runbook) works |
| 29 | bench | before an update, note SSH host key fingerprint, `/home/root/.ssh`, `/home/boat`, Wi-Fi, a BT pairing, hostname, `boat-sleep.key`, passwords; compare after the update and after a rollback |
| 30 | bench | SC7 sleep + WoL wake on slot A and on slot B |
| 31-33 | auto + bench | publish to `bench`: background download, "ready", nothing installs by itself; pull the cable / sleep / reboot mid-download; serve a corrupted file |
| 34, 43, 45 | auto + bench | indexes signed wrongly, signed by the bench CA (on a boat image), replayed, for the wrong channel, with an expired index certificate |
| 35 | auto + bench | indexes offering older / same / other-slot / failed versions, another `compatible`, another L4T |
| 36, 51 | bench | cellular link, and a 4G router on Ethernet marked metered: check only, download waits |
| 37 | bench | clock at 1970: check deferred, no TLS error spam, runs after sync |
| 38 | bench | `/data` filled to within 2 GiB, then unmounted: download refused with the reason |
| 39 | bench | a channel requiring a client certificate: check, download and stream work; the key never in `journalctl` or `ps`; still works after an update and a rollback |
| 40-42, 52 | bench | streamed install of a one-package change (record the bytes in docs/07), a link drop mid-stream, a server without Range, and a stream right after a cold boot |
| 44 | bench | upload a bundle without its index: boards keep the previous index, no alarm |
| 46, 47 | auto + bench | a server streaming past `size`; a redirect to another host / to `http` |
| 48 | bench | `boat-update download` while the timer's run is downloading: the second waits on the lock |
| 49 | bench | sleep/resume and link up/down: a check on each, at most one per 30 minutes |
| 50 | auto + bench | during a trial: `check`/`download` refused until commit |

## Results

*(Filled in on the bench, in the PR that turns `AB_LAYOUT` on by default.
The soak gets one line per delivery - `ssh`, `ota`, and `ota-stream` if
run - from its CSV.)*

| # | date | release | result | notes |
|---|---|---|---|---|
