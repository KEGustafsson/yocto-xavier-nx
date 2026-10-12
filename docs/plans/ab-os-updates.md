# Plan: A/B OS updates with automatic fallback

**Status: DRAFT for review. Nothing here is implemented yet.** This is the
plan to review *before* any code is written. Section 10 lists the decisions
that need an answer first, and section 11 records what a review pass changed.

Scope: signed, power-fail-safe OS updates for the boat computer (Xavier NX
devkit, SD-slot module P3668-0000, rootfs on NVMe, no SD card). Two complete
root filesystems ("slots") live on the SSD. An update is written to the slot
that is not running. The board then tries it **once**, and goes back to the
old slot by itself if the new one does not come up healthy.

---

## 1. Goals, non-goals, success criteria

**Goals**

1. **Safe.** No single power cut, crash or bad bundle can leave the board
   unbootable. Every update either completes and is committed, or the board
   ends up on the previous slot with nobody touching it.
2. **Secure.** Only bundles signed by a key this project controls can be
   installed. No new listening network service, and no private key ever
   lands in git.
3. **Automatic failover.** "Did not boot correctly" is defined precisely
   (section 5.4). Every way of not booting correctly (panic, hang, a rootfs
   that won't mount, an emergency shell, a failed health check) turns into a
   reboot, and that reboot lands on the old slot.
4. **Operable from the chart table.** You install with one command over SSH or
   from a USB stick, `boat-update status` tells you what happened, and you
   roll back with one command.

**Non-goals for this plan** (each one is called out again where it matters)

- **In-field firmware (QSPI bootloader) updates.** On this hardware they
  cannot be made A/B-safe (section 3). Firmware stays a USB-recovery
  operation, as it is today.
- UEFI Secure Boot, fuse burning, dm-verity or a read-only rootfs. These are
  compatible with the design and listed as a later phase, but they are not
  needed for signed updates and the fuse step is irreversible.
- App/container updates. Those already move independently via compose files
  on `/data` (docs/05 "Updates").
- An update server or fleet management (Mender/hawkBit). The design leaves
  room for one (RAUC streams over HTTPS) but does not build one.

**Done means** the fault-injection matrix in section 7 passes on real
hardware, including power cuts during install and during the first boot.

---

## 2. Baseline: what the board does today (relevant facts)

From this repo:

- One rootfs partition `APP` (16 GiB, `ROOTFS_SIZE_BYTES`) on
  `/dev/nvme0n1p1`, flashed with `initrd-flash`. `make-sdcard` gives the
  *last* partition a "fill to end" size, so after a flash `APP` covers the
  whole SSD. `boat-grow-rootfs --grow` then grows the ext4 into it and can
  carve a swap partition at the tail.
- `/data` is **not** a partition yet. Docker's `data-root` already points at
  `/data/docker`, and `10-data-root.conf` already makes dockerd wait for a
  `/data` mount, so the docker side is ready for one.
- No RTC battery is assumed: the clock reads 1970 until the first NTP sync
  (docs/05 "Time without a GPS or RTC"). This matters for certificate checks.
- The `watchdog` daemon feeds the Tegra WDT. Holding the WDT across SC7 is
  confirmed harmless (docs/05). `boat-sleep` honours systemd inhibitors
  unless `--force` is given.

From the upstream code this design builds on (sources in the appendix):

- **The kernel is not in the rootfs today.** meta-tegra leaves
  `UBOOT_EXTLINUX` empty, so no `/boot/extlinux/extlinux.conf` is installed.
  L4TLauncher tries GRUB first, then extlinux, then falls back to `boot.img`
  in the `kernel` partition. The kernel and initrd therefore come from the
  `kernel` partition and the DTB from `kernel-dtb`. docs/01's diagram says
  "kernel + DTB + extlinux + rootfs" on APP, which is probably wrong. Phase 0
  checks `ls /boot` on the board.
- **The default boot mode of L4TLauncher is GRUB.** If
  `\EFI\BOOT\grubaa64.efi` exists on the ESP, L4TLauncher loads it after
  writing `\EFI\BOOT\boot.cfg`. That file sets `cbootargs` (the platform
  kernel arguments) and `root_partition_number` for GRUB to `source`. This
  hand-off is something NVIDIA designed in, not a hack, and the
  recommended design uses it.
- meta-tegra's initramfs (`tegra-minimal-init`) already picks the root
  partition per slot: `boot.slot_suffix=` on the kernel command line wins,
  then the `BootChainOsCurrent` UEFI variable. It mounts
  `PARTLABEL=APP${suffix}`.
- **That initramfs never gives up.** Its mount retry loop never increments
  its counter (`init-boot.sh`), and the intended fallback is `exec sh`.
  Either way a rootfs that won't mount means a board that sits there forever:
  no reset, so no failover. This has to be fixed (section 5.5).
- `nv_update_verifier.service` runs `nvbootctrl verify` on every boot,
  unconditionally. This resets the UEFI rootfs retry counter (section 5.3).

---

## 3. The hardware constraint that shapes everything

NVIDIA's built-in A/B for R35 ("unified bootloader + rootfs A/B",
`USE_REDUNDANT_FLASH_LAYOUT = "1"` in meta-tegra) ties the **rootfs slot to
the firmware boot chain**. L4TLauncher boots `APP` on chain A and `APP_b` on
chain B. When a slot runs out of retries, it switches the *firmware* chain
(`BootChainFwNext`) and cold-resets (`L4TRootfsValidation.c`). Firmware
updates arrive as UEFI capsules, which write the inactive chain and switch to
it. OE4T's Mender integration for JetPack 5 works this way.

**On this board, firmware chain B is incomplete.** The Xavier NX QSPI is
32 MiB. NVIDIA's layouts keep only part of chain B in QSPI. The rest
(`secure-os_b`, `eks_b`, `adsp-fw_b`, `rce-fw_b`, `sce-fw_b`, `bpmp-fw_b`,
`bpmp-fw-dtb_b`) lives on the **SD card** (`flash_l4t_t194_spi_sd_p3668*.xml`)
or on the **eMMC** (`..._spi_emmc_p3668*.xml`). This project uses the
QSPI-only template `flash_l4t_t194_qspi_p3668.xml` plus the NVMe layout, and
neither one has those partitions. MB2 loads them and cannot read NVMe anyway.

So, on this exact configuration:

- switching to firmware chain B will fail (BootChainDxe should detect it and
  revert to A, but the board can still end up stuck);
- a UEFI capsule cannot update chain B, because its required image list
  includes `secure-os`, `bpmp-fw` and `bpmp-fw-dtb`, which have no `_b`
  partition;
- **NVIDIA's own rootfs failover would make things worse.** When a slot
  exhausts its retries, the failover switches firmware to the broken chain
  B.

There is also a build-level trap. With `USE_REDUNDANT_FLASH_LAYOUT = "1"`,
meta-tegra derives the internal template name as
`flash_l4t_t194_qspi_p3668_rootfs_ab.xml`. That file does not exist in
R35.6.4, so the build fails until
`PARTITION_LAYOUT_TEMPLATE_DEFAULT_SUPPORTS_REDUNDANT = "1"` is set.

---

## 4. Options considered

| | **A. GRUB hand-off + RAUC (recommended)** | B. NVIDIA unified A/B + RAUC | C. Patched L4TLauncher + RAUC |
|---|---|---|---|
| Hardware change | none | **eMMC module (P3668-0001)**, or a permanent SD card for chain-B firmware | none |
| Who picks the slot | GRUB (on the ESP), using RAUC's stock `grub` backend: `ORDER`/`x_OK`/`x_TRY` in `grubenv` | UEFI, via `BootChainFwNext`, capsule, `nvbootctrl` | patched L4TLauncher plus our own UEFI variable |
| NVIDIA code patched | no | no | yes: `ValidateRootfsStatus` decoupled from the firmware chain |
| Code we have to write | `grub.cfg` (RAUC's documented template), hooks, health check | custom RAUC backend script around `nvbootctrl`/efivars | UEFI C patch plus a custom RAUC backend |
| RAUC knows the booted slot by | `rauc.slot=` on the cmdline (cleanest) | custom backend `get-current`, which only works if the cmdline has **no** `root=` | same as B |
| Kernel + initrd A/B | yes (from the slot's `/boot`) | yes | yes |
| Kernel DTB A/B | **no**: UEFI's DTB from `kernel-dtb`, frozen at flash time | yes | yes (extlinux `FDT`) |
| Firmware A/B in the field | no (USB reflash) | **yes, via capsule** | no (USB reflash) |
| Fits a later UEFI Secure Boot | harder: GRUB and the kernel must be signed and GRUB must verify | native meta-tegra signing | native, but the patched launcher must be re-reviewed |

**Recommendation: A.** It needs no hardware change and no patches to
NVIDIA's boot code. It uses the GRUB hand-off L4TLauncher already ships and
RAUC's most widely deployed bootloader integration. The two things it gives
up both matter little for this board:

- **Firmware and DTB updates in the field.** Xavier stops at JetPack 5 /
  L4T R35.6.x, which is what meta-tegra kirkstone tracks, so a firmware or DTB
  change means a BSP bump. Doing that as a planned USB reflash at the dock is
  reasonable. Nothing in `meta-boat` patches the device tree today.
- **Secure Boot** is not a current goal (section 1).

Choose **B** if you would rather swap the module for the eMMC one and want
over-the-air firmware too. Phases 3–7 of this plan carry over almost
unchanged; only the bootloader backend differs. Keep **C** as the fallback if
the Phase 0 spike shows the GRUB route cannot boot this kernel correctly.

---

## 5. Recommended design (option A)

### 5.1 Boot flow

```text
 QSPI (firmware chain A only, flashed over USB)
   BootROM -> MB1 -> MB2 -> UEFI
                              |
 NVMe ESP (vfat, flashed)     v
   \EFI\BOOT\BOOTAA64.efi = L4TLauncher  (RootfsRedundancyLevel 0: no NVIDIA rootfs A/B)
        | writes \EFI\BOOT\boot.cfg (cbootargs), loads:
        v
   \EFI\BOOT\grubaa64.efi + grub.cfg + grubenv     <- slot choice + try-once logic
        | linux  (APP|APP_b)/boot/Image  ${cbootargs} <KERNEL_ARGS>
        |        root=PARTLABEL=APP${sfx} boot.slot_suffix=${sfx} rauc.slot=${SLOT} panic=10
        | initrd (APP|APP_b)/boot/initrd
        v
   tegra-minimal-initramfs -> mounts PARTLABEL=APP${sfx} -> systemd
        v
   boot-complete.target <- boat-health-check   ->  rauc-mark-good / mark-bad + reboot
```

If `grubaa64.efi` is missing or broken, L4TLauncher's own fallback chain still
boots slot A: extlinux on `APP`, then `boot.img`. That is the last resort and
it needs nothing from us. It is also why `UBOOT_EXTLINUX = "1"` is kept: the
same recipe (`l4t-launcher-extlinux`) is what puts `/boot/Image` and
`/boot/initrd` into the rootfs for GRUB to read.

### 5.2 Disk layout (custom external layout, `boat-nvme-ab.xml`)

This is NVIDIA's `flash_l4t_nvme_rootfs_ab.xml` with two changes:

- `APP_b` gets **no** filename, so it is not written at flash.
- `SWAP` and `DATA` partitions are added at the end.

| # (GPT id) | name | size | written at flash? | notes |
|---|---|---|---|---|
| … | kernel, kernel-dtb, kernel_b, kernel-dtb_b, recovery, `esp`, `esp_alt`, UDA, … | as NVIDIA | as NVIDIA | unchanged; `esp` gets GRUB added |
| 1 | `APP` | `ROOTFS_SIZE_BYTES` (16 GiB) | yes | slot A |
| 2 | `APP_b` | same | **no** | slot B. Starts with `B_OK=0`, so it is never booted until the first update writes it. This **halves flash time**: one 16 GiB USB 2.0 write instead of two. |
| n | `SWAP` | 8 GiB | no | `mkswap` on first boot, only if the partition has no signature |
| n+1 | `DATA` | min 2 GiB, **fill-to-end** (`0x808`) | small ext4 seed image via `DATAFILE` | first boot runs an online `resize2fs` to fill the partition |

Points that need care:

- **`TEGRA_EXTERNAL_DEVICE_SECTORS`** must become
  `(2 × ROOTFSPART_SIZE + swap + DATA_min + 1 GiB) / 512`. The current
  `ROOTFSPART_SIZE + 1 GiB` would fail at flash time. All three copies of
  that expression (`02-configure-build.sh`, `config/local.conf.sample`,
  `kas/…`) move together.
- **Do not set `USE_REDUNDANT_FLASH_LAYOUT = "1"`.** It would install the
  `RootfsRedundancyLevel = 1` UEFI overlay, which turns on the chain-coupled
  failover from section 3, and it derives a template name that doesn't
  exist. Instead, select the custom file with `PARTITION_LAYOUT_EXTERNAL`
  and install it from a `tegra-bootfiles` bbappend.
- **Slot size is fixed for the life of the layout.** Changing it later needs a
  full reflash. 16 GiB per slot keeps about 3× headroom over today's ~5 GiB
  image. With Docker on `/data`, `IMAGE_ROOTFS_EXTRA_SPACE` (4 GiB) can go.
  RAUC's `resize=true` grows a smaller ext4 image to fill the slot.
- **Both slots carry an identical ext4 UUID and label**, because they come
  from the same image. Nothing may refer to the rootfs by filesystem UUID:
  use PARTLABEL or the GPT entry number only. GRUB addresses the slots as
  `($disk,gpt1)` and `($disk,gpt2)`, with `$disk` taken from the ESP's own
  `$root`. GRUB 2.06 cannot search by partition label.
- **`boat-grow-rootfs` must not run on this layout.** `APP` is no longer the
  last partition, and growing it would break slot symmetry. It is removed
  from the A/B image and given a hard refusal if `APP_b` exists. Its job
  (swap, using the rest of the disk) moves to `boat-storage`, which only ever
  grows `DATA`'s filesystem and never edits the partition table.
- **Fstab and mounts ship in the image** (mount units by PARTLABEL). Today's
  runtime edits to `/etc/fstab` would be lost on the next update, because
  `/etc` belongs to the slot.

### 5.3 Slot selection and boot counting

The ESP `grubenv` holds RAUC's stock variables plus one of ours:

| variable | meaning |
|---|---|
| `ORDER` | e.g. `"B A"`; GRUB tries slots in this order |
| `A_OK` / `B_OK` | slot is usable at all (RAUC sets 0 before writing it) |
| `A_TRY` / `B_TRY` | set to 1 by GRUB just before booting a slot; reset to 0 by `mark-good` |
| `BOAT_TRIAL` | `A`/`B`/empty. Set by our install handler; marks the next boot of that slot as a **trial**. Kept on the ESP (not `/data`) so trial detection never depends on `/data` mounting. |

GRUB logic (RAUC's documented template): for each slot in `ORDER`, if
`OK=1` and `TRY=0`, set `TRY=1`, save, and boot it. If `TRY=1`, the last
boot of that slot never reached `mark-good`, so set `OK=0` and try the next
slot. **The trial boot gets exactly one attempt.** If no slot qualifies:
boot `A` anyway and log it loudly, which beats an EFI shell on a boat.

The initial `grubenv`, built into the ESP image, is
`ORDER="A B" A_OK=1 A_TRY=0 B_OK=0 B_TRY=0`. Every reflash resets it, which
is correct because `APP_b` is not written at flash.

NVIDIA's own counter keeps running underneath. L4TLauncher at
`RootfsRedundancyLevel 0` decrements a 2-bit retry count in a PMC scratch
register on every boot of "slot A". If it reaches 0, it sets
`RootfsStatusSlotA` to unbootable and boots the recovery kernel.
`nv_update_verifier` resets the count on every boot that reaches userspace,
so this only trips after 3 consecutive warm resets that never get there,
which means both GRUB slots are broken. Leave it in place as the outer safety
net. Phase 0 checks two things: that a cold power-on reinitialises the
register, and what the recovery partition actually contains on a meta-tegra
flash (possibly nothing, which would make this outcome "stops in UEFI,
reflash").

### 5.4 What "booted correctly" means

There are two tiers, so that a broken *data* partition or an app problem can
never flip the OS back to an older slot.

- **Tier 1, required on every boot before `mark-good`:**
  - the kernel booted the slot named by `rauc.slot`;
  - the rootfs is mounted read-write from the matching `APP${sfx}`;
  - `multi-user.target` was reached with no emergency/rescue mode;
  - the ESP is mounted and `rauc status` works.
- **Tier 2, required only on trial boots (`BOAT_TRIAL` == booted slot):**
  - critical units are active: `NetworkManager`, `sshd`/`sshd.socket`,
    `systemd-logind`, `dbus`, and `docker` if `/data` is mounted;
  - no failed units in a configurable critical list;
  - optional operator checks in `/data/boat-update/health.d/*`, each with a
    timeout (e.g. "the Signal K container answers"). **Off by default.**
- **Never part of either tier:** anything external, such as the internet,
  NTP, GNSS, registry pulls, or NMEA traffic.

Mechanics, all stock systemd 250:

- `boat-health-check.service` is `Before=` and `RequiredBy=`
  `boot-complete.target`.
- meta-rauc's `rauc-mark-good.service` already has `Requires=` and `After=`
  `boot-complete.target`, plus `ConditionKernelCommandLine=|rauc.slot`, which
  our GRUB command line satisfies.
- `boot-complete.target` gets `JobTimeoutSec=10min` and
  `JobTimeoutAction=reboot`, so a health check that hangs still ends in a
  reboot.
- On a Tier 2 failure, `OnFailure=` runs `rauc status mark-bad booted`,
  records the reason on `/data/boat-update/last-result` (best effort), and
  reboots. GRUB then skips the bad slot.
- On success, mark-good clears `x_TRY`, then `BOAT_TRIAL` is cleared. The
  update is now **committed**.

`nv_update_verifier.service` keeps running unconditionally. It only maintains
NVIDIA's slot-A counter from 5.3 and has nothing to do with our slot
choice.

### 5.5 Making every failure end in a reboot

A failover only happens on the *next* boot, so every way of failing has to
end in a reset:

| failure | what turns it into a reset | change needed |
|---|---|---|
| firmware fails before UEFI | not covered (single chain). Firmware never changes in the field. | none |
| GRUB file corrupt or missing | L4TLauncher falls back to extlinux on `APP`, then `boot.img` | none |
| GRUB → kernel hangs before ExitBootServices | UEFI watchdog, armed by L4TLauncher for 5 min before `StartImage` | none (verify) |
| kernel panic | `panic=10` on the GRUB command line | add |
| kernel hard hang | Tegra hardware WDT running from early kernel | **Phase 0: is it already armed before userspace?** If not, enable it at probe (DT/module option) |
| initramfs can't find or mount the rootfs | today: **hangs forever** (infinite loop / `exec sh`) | bbappend `tegra-minimal-init`: bounded retries, then `reboot -f` (an `init.debug` cmdline flag keeps the shell for bench work) |
| systemd emergency/rescue shell | today: waits for a password forever | drop-ins: on a headless boat, emergency and rescue reboot after 60 s unless `/data/boat-update/stay-in-emergency` exists |
| PID 1 / userspace wedged | systemd `RuntimeWatchdogSec=60s`, `RebootWatchdogSec=10min` | **decision D8**: only one opener of `/dev/watchdog` |
| health check hangs | `JobTimeoutAction=reboot` on `boot-complete.target` | add |
| power cut during trial | `B_TRY=1` is already saved, so the next boot skips B | none (conservative: counts as a failure) |

### 5.6 The install sequence, and what a power cut does at each step

`boat-update install <bundle|URL>` is a thin wrapper around `rauc install`.
It holds a systemd **sleep + shutdown block inhibitor** for the whole install,
so `boat-sleep` (and the UDP listener) refuses to suspend mid-write.
`boat-sleep --force` would still bypass it, so make it also check for an
update in progress.

| step | who | if power is lost here |
|---|---|---|
| 1. verify signature, format (`verity` only), `compatible`, version policy | RAUC + `pre-install` handler | nothing changed |
| 2. set target slot `OK=0` | RAUC grub backend | target can't be booted; you stay on the current slot |
| 3. write image to `APP${target}`, verify | RAUC (`type=ext4`, `resize=true`) | target half-written, still `OK=0`, never booted |
| 4. slot post-install hook: carry over state into the new slot (5.8), optional read-back hash check | our hook | target still `OK=0` |
| 5. activate: `ORDER=target first`, `OK=1`, `TRY=0` | RAUC | `grubenv` is rewritten via `grub-editenv`. A cut mid-write can lose the env, in which case GRUB defaults to `A B` with both `OK=1`. That boots a complete slot either way; the `TRY` logic still catches a bad one, so at worst one boot is wasted. |
| 6. set `BOAT_TRIAL=target`, record pending version on `/data` | `post-install` handler | trial marker missing means Tier 2 is skipped once; Tier 1 still gates |
| 7. reboot (by the operator, or `--reboot`) | | |
| 8. trial boot → commit, or mark-bad → fallback | 5.4 | `B_TRY=1` → fallback (5.5) |

Manual rollback is `boat-update rollback`, which runs
`rauc status mark-active other` and reboots. It refuses if the other slot is
`OK=0`. Because the old slot still holds its own complete rootfs, rollback is
just a slot switch. Nothing is reinstalled.

### 5.7 Security model

- **Bundle authenticity:** RAUC CMS signatures (X.509), keyring
  `/etc/rauc/ca.cert.pem` in each slot. `[system] bundle-formats=-plain`, so
  only **verity** bundles are accepted: dm-verity protects the payload after
  the signature check, which closes the check-then-use gap and is also what
  makes HTTPS streaming safe later. `check-purpose=codesign` with a signing
  cert that has the codeSigning EKU.
- **Clock:** there is no RTC, so the clock may read 1970 at install time, and
  certificate-validity checks would reject a valid bundle. Set
  `use-bundle-signing-time=true`. The signing time is a signed attribute, so
  only the key holder can set it.
- **Keys never in git:** `scripts/gen-update-keys.sh` creates a development
  CA and signing key under `${WORKROOT}/keys/rauc/` (already git-ignored as
  `/yocto/`, mode 600). `local.conf` points `RAUC_KEY_FILE`, `RAUC_CERT_FILE`
  and `RAUC_KEYRING_FILE` there. **The build fails** if they are missing;
  meta-rauc's example CA only produces a warning, and an image that trusts
  the public example key is the worst possible outcome. `lint.sh` gains a
  check that no tracked file contains `PRIVATE KEY-----`. (The
  `meta-rauc-tegra` demo layer commits its development key; this is the
  opposite stance.)
- **Production keys:** the CA private key is kept offline. The signing key
  can sit on a PKCS#11 token (RAUC supports it). Rotation: ship a keyring
  holding old and new CA in an update signed by the old key, then switch.
- **Compatibility and downgrade:** `compatible=boat-xavier-nx` rejects
  bundles for other boards. The `pre-install` handler rejects a bundle whose
  version is lower than the booted slot's unless explicitly overridden
  (**decision D6**). Falling back after a failed update is a slot switch,
  not an install, so the policy never blocks a failover.
- **Attack surface:** no update daemon and no new port. Installs start over
  the existing SSH or from local media. RAUC's D-Bus API is root-only.
- **Honest limits:** this authenticates *updates*. Root on the running board
  can still write anything to disk, and the bench image's shared password
  (README "Credentials") still applies. A physical attacker with USB
  recovery mode can reflash. Closing those gaps is the Secure Boot,
  dm-verity and credential-lockdown phase (6, Phase 7). For option A that
  phase must also sign GRUB and have it verify the kernel. GRUB's
  interactive command line can edit kernel arguments, so `timeout=0` plus a
  GRUB superuser password close the console path cheaply.

### 5.8 State that must survive a slot switch

`/etc` and `/var` belong to the slot, and an update replaces them. Two
mechanisms handle that:

- **Carry-over at install time:** the slot post-install hook copies a curated
  list from the running slot into the freshly written one. This is
  early-boot safe and keeps each slot self-contained, so a rollback sees that
  slot's own consistent copy. The initial list:
  - `/etc/machine-id` (journal continuity, NetworkManager DHCP identity);
  - `/etc/ssh/ssh_host_*` (no host-key change on every update);
  - `/etc/boat-sleep.key`;
  - `/etc/NetworkManager/system-connections/` (Wi-Fi PSKs);
  - `/var/lib/bluetooth/` (pairings);
  - `/etc/hostname` and `/etc/localtime`;
  - the operator-edited `CONFFILES` (`/etc/default/boat-power`,
    `/etc/default/boat-bluetooth`, `/etc/docker/daemon.json`);
  - the password hash fields of `root` and `boat` only, merged rather than
    copied whole (**decision D5**).
- **Lives on `/data`:**
  - Docker (already);
  - compose (already);
  - RAUC's `data-directory` (`/data/rauc`);
  - `boat-update` history;
  - a size-capped persistent journal (`/data/log/journal`, bind-mounted, so
    `journalctl -b -1` after a fallback shows *why* the trial failed).

Documented consequence: local edits to any other `/etc` file do not survive
an update. Put them in the image (`meta-boat`) or on `/data`. If the rootfs
later becomes read-only (Phase 7), carry-over turns into an overlay or bind
mounts.

Also documented: `/data` is shared, so a newer Docker could migrate
`/data/docker` in a way an older slot can't read. Within kirkstone, Docker
moves little. The `pre-install` handler records both Docker versions so it
can warn.

### 5.9 What stays outside A/B, deliberately

| what | why | how it changes |
|---|---|---|
| QSPI firmware (one usable chain) | section 3 | `05-flash-nvme.sh` (USB recovery) |
| kernel DTB (`kernel-dtb` partition, read by UEFI) | option A's trade-off | USB reflash; Phase 0 confirms GRUB passes UEFI's DTB through unchanged |
| ESP: L4TLauncher, GRUB, `grub.cfg` | single copy, written at flash | USB reflash. `grubenv` and `boot.cfg` are runtime-written, small, in place. |
| `kernel`/`kernel_b` partitions (`boot.img`) | only the L4TLauncher last-resort fallback | flash time only |

A BSP bump (new meta-tegra L4T) is therefore always a **full USB reflash**,
not an update bundle. The `pre-install` handler rejects a bundle whose
`/etc/nv_tegra_release` differs from the running one.

---

## 6. Implementation phases

Each phase ends with something testable on the bench. Nothing ships to the
boat before Phase 6 passes.

**Phase 0: hardware spike, no repo changes (bench board, USB recovery at hand).**
Answer these before writing recipes; any "no" re-opens section 4.

1. Capture: `/proc/cmdline`, `ls /boot`, `cat /proc/mtd`,
   `nvbootctrl dump-slots-info`, `sgdisk -p /dev/nvme0n1`, ESP free space,
   `zcat /proc/config.gz | grep -E 'DM_VERITY|BLK_DEV_LOOP|SQUASHFS|BLK_DEV_NBD'`.
2. GRUB hand-off: copy a hand-built `grubaa64.efi` and `grub.cfg` onto the
   running board's ESP (`/boot/efi/EFI/BOOT/`; no reflash needed). Boot the
   kernel and initrd from `APP` with `${cbootargs}`. Check:
   - the HDMI/XFCE desktop, Wi-Fi/Bluetooth, GPU (`docker run --runtime
     nvidia`), fan/nvpmodel;
   - **an SC7 sleep plus WoL wake**;
   - that `/proc/device-tree` is identical to a non-GRUB boot (DTB passed
     through).
3. Watchdog: `cat /sys/class/watchdog/watchdog0/{state,timeout}` early in
   boot. `echo c > /proc/sysrq-trigger` with `panic=10`: does the board come
   back? Does a hard hang come back?
4. NVIDIA counter: confirm the scratch register re-initialises on a cold
   power-on, and see what L4TLauncher does with an empty recovery partition.
5. (Optional, bench only, needs USB recovery ready) confirm section 3:
   `nvbootctrl set-active-boot-slot 1` and watch the chain-B switch fail and
   revert.

**Phase 1: layout, GRUB, slot-aware boot.** Effort: M.
- `env.sh`: `AB_LAYOUT` (default **off** until Phase 6), swap size, DATA
  minimum.
- `02-configure-build.sh`, `config/local.conf.sample`, `kas/xavier-nx-nvme.yml`:
  - `PARTITION_LAYOUT_EXTERNAL`;
  - the new `TEGRA_EXTERNAL_DEVICE_SECTORS` expression;
  - `UBOOT_EXTLINUX = "1"`;
  - `IMAGE_TEGRAFLASH_DATA`/`DATAFILE`;
  - all three kept identical, as today.
- `meta-boat`:
  - `tegra-bootfiles` bbappend installing `boat-nvme-ab.xml`;
  - `boat-grub-esp` (`grubaa64.efi` from `grub-efi`, `grub.cfg` generated
    from `KERNEL_ARGS` so the two can't drift, a pre-sized 1 KiB `grubenv`);
  - a `tegra-espimage` bbappend adding it;
  - `tegra-minimal-init` bbappend (bounded mount retry, then reboot);
  - kernel fragment `boat-rauc.cfg` (loop, squashfs, dm-verity, nbd), for
    whatever Phase 0 shows is missing.
- **Exit:** a fresh flash boots A through GRUB. A hand-written test image in
  B (`dd`, then `grub-editenv`) boots. A `sysrq-c` during a B trial lands
  back on A.

**Phase 2: storage and state.** Effort: S–M.
- `boat-storage` (first boot): grow the `DATA` ext4 online, `mkswap` an empty
  `SWAP`, both idempotent and guarded by exact PARTLABEL plus a "no existing
  signature" check.
- `data.mount`/swap units with `nofail`. A missing `/data` degrades Docker
  but never fails Tier 1.
- Persistent journal on `/data`, size-capped.
- `boat-grow-rootfs`: refuse on A/B layouts, and drop it from the A/B image.
- **Exit:** the first boot after a flash has `/data` filling the SSD and
  8 GiB of swap. A reboot changes nothing.

**Phase 3: RAUC.** Effort: M.
- `01-fetch-layers.sh` + kas: add `meta-rauc` (kirkstone ships RAUC 1.13).
- `bblayers` in all three places; `LAYERRECOMMENDS_boat += "rauc"`.
- RAUC-specific appends under `layers/meta-boat/external/rauc/`, the layer's
  existing pattern.
- `DISTRO_FEATURES += "rauc"`.
- `rauc` bbappend with `system.conf`: `bootloader=grub`,
  `grubenv=/boot/efi/EFI/BOOT/grubenv`, slots `rootfs.0`/`rootfs.1` by
  PARTLABEL with `bootname=A`/`B`, `data-directory=/data/rauc`, keyring
  options as in 5.7.
- `grub-editenv` on the target.
- `scripts/gen-update-keys.sh`.
- `boat-bundle.bb` (`inherit bundle`, `RAUC_BUNDLE_FORMAT = "verity"`, the
  rootfs slot from `boat-image`, hooks).
- Handlers and hooks: `pre-install` (version/BSP/compatible policy, inhibitor
  check), slot `post-install` (carry-over plus optional read-back hash),
  system `post-install` (`BOAT_TRIAL`, pending record).
- **Exit:** `rauc install` of a bundle writes B, the board reboots into B, and
  `rauc status` is right on both slots.

**Phase 4: health gate and reboot coverage.** Effort: M.
- `boat-health-check` (Tier 1/2, config in `/etc/boat-update/health.conf`).
- `boot-complete.target` timeout drop-in; `OnFailure` handler.
- Emergency/rescue drop-ins.
- Watchdog consolidation (D8), re-tested across SC7 because it touches
  `boat-power`'s sleep hook.
- **Exit:** every row of 5.5 except firmware has been exercised once.

**Phase 5: operator tooling and docs.** Effort: S–M.
- `boat-update`, on the device. `status` (default, read-only, in the style
  of the other `boat-*` tools) shows:
  - which slot is booted;
  - committed or trial;
  - the last result and why;
  - both slots' versions.

  Plus `install [--reboot]`, `rollback`, `history`.
- `scripts/push-update.sh <host> [bundle]`, on the host: copy, install,
  reboot, then *wait and confirm which slot came back*, like
  `wol/boat-wake.sh`'s "did it come back?" half.
- `docs/07-updates.md`. Updates to:
  - docs/01: boot flow;
  - docs/04: layout, flash time, the first A/B flash erases everything;
  - docs/05: "Updates" and "Reclaiming the rest of the SSD";
  - docs/06: troubleshooting table;
  - README.

**Phase 6: validation.** Effort: M, mostly bench time. Run the section 7
matrix. Then soak: 50 update cycles alternating good and deliberately broken
bundles. If you have a USB relay on the barrel jack, add random power cuts.
Then flip `AB_LAYOUT` to default-on.

**Phase 7 (later, separate plans):**
- HTTPS streaming plus RAUC adaptive updates, so only changed blocks cross a
  cellular link;
- Secure Boot, dm-verity and a read-only rootfs;
- credential lockdown;
- option B, if the hardware ever moves to the eMMC module.

---

## 7. Test plan: the acceptance matrix

All on hardware. QEMU cannot emulate Tegra UEFI. The pure-shell parts
(handlers, `boat-update`, `boat-storage`) also get small host-side tests with
`rauc`, `grub-editenv` and `blkid` mocked, run from `lint.sh`, so the logic
is covered on every PR.

| # | scenario | expected |
|---|---|---|
| 1 | good bundle, normal reboot | boots new slot, Tier 2 passes, committed; `boat-update status` shows the new version |
| 2 | bundle signed by an unknown key | rejected before anything is written |
| 3 | `plain` (non-verity) bundle | rejected |
| 4 | wrong `compatible` | rejected |
| 5 | older version, no override | rejected; with override, installs |
| 6 | bundle built on a different L4T | rejected (5.9) |
| 7 | clock at 1970 (no NTP), valid bundle | installs (`use-bundle-signing-time`) |
| 8 | power cut during the slot write | boots old slot; target `OK=0`; a re-install works |
| 9 | power cut between activate and reboot | boots new slot as a trial (or old slot if env lost); never unbootable |
| 10 | power cut during the trial boot | next boot on old slot; new slot marked bad; `status` says so |
| 11 | new slot: kernel panic (test bundle with a bad cmdline/initrd) | `panic=10` reset → old slot |
| 12 | new slot: rootfs won't mount (test bundle with corrupted ext4) | initramfs reboots → old slot |
| 13 | new slot: emergency mode (test bundle with a bad mount unit) | reboot after 60 s → old slot |
| 14 | new slot: Tier 2 failure (test bundle with `docker` masked) | mark-bad + reboot → old slot, reason recorded |
| 15 | new slot: hard hang (test bundle: sysrq freeze) | WDT reset → old slot |
| 16 | committed slot, `/data` corrupted/missing | stays on committed slot (Tier 1 only); Docker degraded; alert in `status` |
| 17 | `boat-update rollback` after a committed update | boots other slot, which is then committed |
| 18 | `boat-sleep` (and UDP sleep) during install | refused |
| 19 | both slots broken | documented outcome (boots A per the GRUB last-resort rule; worst case UEFI recovery/reflash). Verify it is *not* a reset loop. |
| 20 | carry-over | SSH host key, Wi-Fi, BT pairing, hostname, `boat-sleep.key` and passwords (per D5) unchanged after update and after rollback |
| 21 | SC7 + WoL on both slots | works |
| 22 | GRUB missing from ESP | L4TLauncher fallback boots A |

Test bundles for 11–15 come from a `boat-image-faulty` variant built only on
request (`BOAT_FAULT=<kind>`). They are signed with the dev key and can never
be produced by a normal build.

---

## 8. Migrating existing boards

The partition layout changes, and `APP` currently covers the whole SSD, so
**the first A/B install is a one-time full USB reflash that erases the SSD**.
There is no safe in-place conversion and the plan does not attempt one.
Procedure (lands in docs/07):

1. On the old system, back up `/data` (compose, volumes, Signal K state) and
   any hand-edited `/etc` files to a laptop with `rsync`.
2. Full flash with `AB_LAYOUT=1` (`05-flash-nvme.sh`, not
   `--skip-bootloader`: the ESP gains GRUB).
3. First boot: `boat-storage` provisions `/data` and swap. Restore `/data`.
4. Install a bundle once (A→B), then once more (B→A), to prove the round trip
   before the boat leaves the dock.

---

## 9. Risks and open questions

| risk | likelihood / impact | mitigation |
|---|---|---|
| GRUB on this UEFI mishandles the DTB, initrd or cmdline (SC7, display) | M / H | Phase 0 step 2 before anything else; option C is the fallback |
| Tegra WDT not armed until userspace, so an early kernel hang never resets | M / M | Phase 0 step 3; enable at probe, or accept and document power-cycle-to-recover |
| `grubenv` lost by a power cut during `grub-editenv` on vfat | L / L | GRUB defaults to a complete slot; `TRY` logic still applies |
| an ESP (vfat) fault makes the board unbootable | L / H | ESP writes are tiny and rare; fsck.fat check in `boat-update status`; recovery = USB reflash |
| a shared `/data` written by a newer slot breaks an older one | L / M | version warnings; Docker stays at the kirkstone major |
| update bundle size over cellular (several GB with CUDA) | H / M | ship over shore Wi-Fi now; adaptive streaming in Phase 7 |
| meta-rauc kirkstone and this repo's host workarounds (Python 3.14, gcc-12) collide | M / L | it is the same bitbake; build `rauc-native` early in Phase 3 |
| flash time | — | unchanged, since `APP_b` is not written at flash |

---

## 10. Decisions needed before coding

| # | question | recommendation |
|---|---|---|
| D1 | Architecture: A (GRUB hand-off, current hardware), B (eMMC module, NVIDIA-native incl. firmware OTA), or C (patch L4TLauncher)? | **A** |
| D2 | Slot size | keep **16 GiB** per slot |
| D3 | Firmware and DTB stay USB-reflash-only | **yes** (Xavier's BSP line is at its end) |
| D4 | One-time erase-and-reflash migration | **yes**, with the backup procedure in section 8 |
| D5 | Carry passwords over to the new slot, or reset them to the image's `BOAT_PASSWORD_HASH`? | **carry over** (merge the hash only) |
| D6 | Downgrades: reject by default with an explicit override? | **yes** |
| D7 | Transport for now | SSH push + USB stick; HTTPS streaming later |
| D8 | Watchdog: systemd `RuntimeWatchdogSec` (covers PID 1 from the start of boot) instead of the `watchdog` daemon opening the device? | **systemd**. Keep the daemon only if its custom checks are used (they aren't today). |
| D9 | Key custody: dev keys local; production CA offline and signing key on a token? | **yes** |
| D10 | Trial gets one attempt (power cut during trial = rollback) | **yes**; retry is one command |

---

## 11. Review log

*(Filled in by the review pass, then by your review.)*

---

## Appendix: evidence (upstream sources this plan was checked against)

Revisions as fetched for this plan (meta-tegra kirkstone `47519b9`,
L4T R35.6.4; edk2-nvidia `r35.6.4-updates` `942f38f`; meta-rauc kirkstone
`4cb7c47`, RAUC 1.13; meta-mender-community kirkstone).

- Firmware chain-B placement: `Linux_for_Tegra/bootloader/t186ref/cfg/`
  `flash_l4t_t194_qspi_p3668.xml` (no `secure-os_b`, `bpmp-fw_b`, …),
  `flash_l4t_t194_spi_sd_p3668.xml` (on `sdcard`),
  `flash_l4t_t194_spi_emmc_p3668_rootfs_ab.xml` (on `sdmmc_user`). No
  `flash_l4t_t194_qspi_p3668_rootfs_ab.xml` exists.
- NVMe A/B layout: `Linux_for_Tegra/tools/kernel_flash/flash_l4t_nvme_rootfs_ab.xml`.
  `APP` has id 1, `APP_b` id 2, there is no fill-to-end attribute, and
  `make-sdcard.sh` `find_finalpart` skips the "last partition fills the disk"
  rule when `APP_b` exists.
- Rootfs/firmware coupling: edk2-nvidia
  `Silicon/NVIDIA/Application/L4TLauncher/L4TRootfsValidation.c`
  (`ValidateRootfsStatus`, `RF_FW_NEXT`, `ResetCold`). The retry count is
  2 bits in a scratch register (`L4TRootfsValidation.h`).
- Boot modes, GRUB path and `boot.cfg`: `L4TLauncher.c` (`ProcessBootParams`,
  default `NVIDIA_L4T_BOOTMODE_GRUB`, 5-minute `SetWatchdogTimer`) and
  `L4TLauncher.h` (`GRUB_PATH`, `GRUB_BOOTCONFIG_CONTENT_FORMAT`).
- Firmware chain switching: `Silicon/NVIDIA/Drivers/BootChainDxe/BootChainDxe.c`.
- Capsule image list: `Silicon/NVIDIA/Library/FmpDeviceLib/FwImageNames.c`.
  Default capsule signing uses EDK2 *test* certs:
  meta-tegra `classes/tegra-uefi-capsule-signing.bbclass`.
- meta-tegra: `conf/machine/include/tegra-common.inc` (redundant-layout
  variables, `UBOOT_EXTLINUX ?= ""`),
  `recipes-core/initrdscripts/tegra-minimal-init/{init-boot.sh,platform-preboot.sh}`,
  `recipes-bsp/tegra-binaries/tegra-redundant-boot/nv_update_verifier.service`,
  `recipes-bsp/uefi/{tegra-espimage.bb,l4t-launcher-extlinux.bb}`.
- RAUC 1.13: booted-slot detection order (`src/context.c`:
  `rauc.slot=` → `root=` → custom `get-current`), refusal to write a mounted
  slot (`src/install.c`), `docs/integration.rst` (custom and grub backends),
  meta-rauc `rauc-mark-good.service`.
- Prior art: meta-mender-community
  `meta-mender-tegra/meta-mender-tegra-jetpack5` (capsule-driven switch,
  `nvbootctrl verify`, gated `nv_update_verifier`). Supported boards there are
  AGX Orin, AGX Xavier and Orin Nano, all of which have eMMC for chain B. Also
  meta-rauc-community `meta-rauc-tegra` (U-Boot/R32 boards only, so not
  applicable).
