# Plan: A/B OS updates with automatic fallback

**Status: DRAFT v2 for review. Nothing here is implemented yet.** This is the
plan to review *before* any code is written. Section 10 lists the decisions
that need an answer first. Section 11 records what the first review pass
found and how v2 answers it.

Scope: signed, power-fail-safe OS updates for the boat computer (Xavier NX
devkit, SD-slot module P3668-0000, rootfs on NVMe, no SD card). Two complete
root filesystems ("slots") live on the SSD. An update is written to the slot
that is not running. The board tries it **once**, and goes back to the
previous slot by itself if the new one does not come up healthy.

---

## 1. Goals, non-goals, success criteria

**Goals**

1. **Safe.** No single power cut, crash or bad bundle leaves the board
   unbootable. Every update either completes and is committed, or the board
   ends up on the previous slot with nobody touching it.
2. **Stable.** A power cut or brownout on a *committed* system must not
   silently move the boat back to an older OS. Only a slot that keeps failing
   hands over to the other one.
3. **Secure.** Only bundles signed by a key this project controls can be
   installed, and only root can start an install or a slot switch. No new
   listening network service. No private key in git.
4. **Automatic failover.** "Did not boot correctly" is defined precisely
   (section 5.4), and every way of not booting correctly ends in a reset
   that lands on the old slot (section 5.5).
5. **Operable from the chart table.**
   - Install with one command over SSH or from a USB stick.
   - `boat-update status` says what happened.
   - Roll back with one command.

**Non-goals** (each is picked up again where it matters)

- **In-field firmware (QSPI bootloader) and kernel-DTB updates.** On this
  hardware they cannot be made A/B-safe (section 3). They stay USB-recovery
  operations, as they are today.
- UEFI Secure Boot, fuse burning, dm-verity, a read-only rootfs. The design
  is compatible with them (Phase 7), but signed updates don't need them, and
  burning fuses is irreversible.
- App/container updates. These already move independently via compose files
  on `/data` (docs/05 "Updates").
- Update servers and fleet management.

**Done means** the fault-injection matrix in section 7 passes on real
hardware, including power cuts during install and during the first boot.

---

## 2. Baseline: what the board does today (relevant facts)

From this repo:

- **One rootfs partition.** `APP` (16 GiB, `ROOTFS_SIZE_BYTES`) on
  `/dev/nvme0n1p1`, flashed with `initrd-flash`. `make-sdcard` makes the
  *last* partition "fill to end", so after a flash `APP` spans the SSD.
  `boat-grow-rootfs --grow` then grows the ext4 and can carve swap at the
  tail.
- **`/data` is not a partition yet.** Docker's `data-root` already points at
  `/data/docker`, and `10-data-root.conf` makes dockerd wait for a `/data`
  mount, so Docker is ready for one.
- **No RTC battery is assumed.** The clock reads 1970 until the first NTP
  sync (docs/05 "Time without a GPS or RTC").
- **The `watchdog` daemon feeds the Tegra WDT.** Holding it across SC7 is
  confirmed harmless. `boat-sleep` honours systemd inhibitors unless given
  `--force`.
- **Containers can reach the system D-Bus.** The Signal K compose example
  bind-mounts `/run/dbus/system_bus_socket` (docs/05). This matters for
  section 5.7.

From the upstream code this design builds on (sources in the appendix):

- **L4TLauncher's default boot mode is DIRECT (extlinux).** NVIDIA's
  `L4TConfiguration.dts` sets `L4TDefaultBootMode = 1`, and meta-tegra
  deploys that overlay by default.
  - L4TLauncher tries extlinux on the slot's `APP` first, then falls back
    to `boot.img` in the `kernel` partition.
  - It only tries GRUB (`\EFI\BOOT\grubaa64.efi`, after writing
    `\EFI\BOOT\boot.cfg` with `cbootargs`) when that variable is 0, or when
    the variable is missing.
  - The DT value only seeds a variable that is *absent*, so a board that
    already has the variable keeps its value.
- **The kernel is not in the rootfs today.** meta-tegra leaves
  `UBOOT_EXTLINUX` empty, so there is no `/boot/extlinux/extlinux.conf`.
  DIRECT fails, and the kernel and initrd come from `boot.img` in the
  `kernel` partition, with the DTB from `kernel-dtb`. docs/01 and docs/04
  say the kernel and extlinux live in `APP`, which is probably wrong.
  Phase 0 checks `ls /boot`.
- **The initramfs already picks the root partition per slot.**
  meta-tegra's `tegra-minimal-init` honours `boot.slot_suffix=`, then the
  `BootChainOsCurrent` UEFI variable, and mounts the *first*
  `PARTLABEL=APP${suffix}` that `blkid` finds. Its last fallback is
  `/dev/mmcblk0p1`.
- **That initramfs never gives up.** Its mount-retry loop never increments
  its counter, so the intended `exec sh` is unreachable. A rootfs that won't
  mount means a board that sits there forever, with no reset and so no
  failover (section 5.5).
- **`nv_update_verifier.service` runs `nvbootctrl verify` on every boot,
  unconditionally.** This is what keeps NVIDIA's rootfs retry counter from
  running out (section 5.3).
- **meta-tegra writes nothing to the recovery partition.** If NVIDIA's
  counter ever trips, L4TLauncher returns without a reset and the board
  **stops in UEFI**.

---

## 3. The hardware constraint that shapes everything

NVIDIA's built-in A/B for R35 ("unified bootloader + rootfs A/B",
`USE_REDUNDANT_FLASH_LAYOUT = "1"` in meta-tegra) ties the **rootfs slot to
the firmware boot chain**. `APP` boots on chain A and `APP_b` on chain B.
When a slot runs out of retries, NVIDIA switches the *firmware* chain
(`BootChainFwNext`) and cold-resets (`L4TRootfsValidation.c`). Firmware
updates arrive as UEFI capsules that write the inactive chain and switch to
it. OE4T's Mender integration for JetPack 5 works this way.

**On this board, firmware chain B is incomplete.**

- The Xavier NX QSPI is 32 MiB, and NVIDIA keeps only part of chain B in it.
- The rest (`secure-os_b`, `eks_b`, `adsp-fw_b`, `rce-fw_b`, `sce-fw_b`,
  `bpmp-fw_b`, `bpmp-fw-dtb_b`) lives on the **SD card**
  (`flash_l4t_t194_spi_sd_p3668*.xml`) or on the **eMMC**
  (`..._spi_emmc_p3668*.xml`).
- This project uses the QSPI-only template `flash_l4t_t194_qspi_p3668.xml`
  plus the NVMe layout, and neither has those partitions. MB2 loads them and
  cannot read NVMe anyway.

So, on this configuration:

- **Switching to firmware chain B fails.** BootChainDxe should detect this
  and revert to A, but that is not something to depend on.
- **A capsule cannot update chain B.** Its required image list includes
  `secure-os`, `bpmp-fw` and `bpmp-fw-dtb`.
- **NVIDIA's rootfs failover would make things worse.** When retries run
  out it switches firmware to the broken chain B.
- **There is also a build trap.** With `USE_REDUNDANT_FLASH_LAYOUT = "1"`,
  meta-tegra derives the internal template name
  `flash_l4t_t194_qspi_p3668_rootfs_ab.xml`, which does not exist in
  R35.6.4.

---

## 4. Options considered

| | **A. GRUB hand-off + RAUC (recommended)** | B. NVIDIA unified A/B + RAUC | C. Patched L4TLauncher + RAUC |
|---|---|---|---|
| Hardware change | none | **eMMC module (P3668-0001)**, or a permanent SD card for chain-B firmware | none |
| Who picks the slot | GRUB on the ESP; RAUC's stock `grub` backend plus our attempt counter in `grubenv` | UEFI, via `BootChainFwNext` / capsule / `nvbootctrl` | patched L4TLauncher plus our own UEFI variable |
| Platform change needed | `L4TDefaultBootMode` 1 → 0 (GRUB). It is a UEFI setting, not a code patch. | none | C patch to `ValidateRootfsStatus` |
| Code we write | `grub.cfg` state machine, hooks, health/commit unit | custom RAUC backend around `nvbootctrl` and efivars | UEFI patch plus custom backend |
| RAUC knows the booted slot by | `rauc.slot=` on the cmdline (cleanest) | custom backend `get-current`, which only works with **no** `root=` on the cmdline | same as B |
| Kernel + initrd A/B | yes (from the slot's `/boot`) | yes | yes |
| Kernel DTB A/B | **no**: UEFI's DTB from `kernel-dtb`, frozen at flash | yes | yes (extlinux `FDT`) |
| Firmware A/B in the field | no (USB reflash) | **yes, via capsule** | no (USB reflash) |
| Later UEFI Secure Boot | harder: GRUB and the kernel must be signed and verified | native meta-tegra signing | native, but the patched launcher must be re-reviewed |

**Recommendation: A.** It needs no hardware change and no patch to NVIDIA's
boot code. It uses the GRUB hand-off L4TLauncher already implements, plus
RAUC's most widely deployed bootloader integration. What it gives up,
firmware and DTB updates in the field, matters little here, because Xavier
stops at L4T R35.6.x. A BSP bump becomes a planned USB reflash at the dock.
Nothing in `meta-boat` patches the device tree today.

Choose **B** if you would rather move to the eMMC module and want
over-the-air firmware. Phases 2–7 carry over; only the boot backend differs.
Keep **C** as the fallback if the Phase 0 spike shows the GRUB route cannot
boot this kernel correctly.

---

## 5. Recommended design (option A)

### 5.1 Boot flow

```text
 QSPI (firmware chain A only; flashed over USB)
   BootROM -> MB1 -> MB2 -> UEFI   (L4TDefaultBootMode = 0: GRUB)
                              |
 NVMe ESP (vfat)              v
   \EFI\BOOT\BOOTAA64.efi = L4TLauncher  (RootfsRedundancyLevel 0)
        | writes \EFI\BOOT\boot.cfg (cbootargs), loads:
        v
   \EFI\BOOT\grubaa64.efi + grub.cfg + grubenv      <- slot choice + attempt counting
        | linux  ($disk,gptN)/boot/Image  ${cbootargs} <KERNEL_ARGS>
        |        root=PARTUUID=<slot> rauc.slot=<A|B> panic=10 [boat.trial=1]
        | initrd ($disk,gptN)/boot/initrd
        v
   tegra-minimal-initramfs (patched: root=PARTUUID only, bounded retry, reboot)
        v
   systemd -> boat-commit.service (health gate) -> rauc mark-good + COMMITTED=1
                                                 or mark-bad + reboot (trial only)
```

**Getting GRUB into the path** (review finding B1):

- Install an `L4TConfiguration` overlay with `L4TDefaultBootMode = 0` in
  place of meta-tegra's (`TEGRA_BOOTCONTROL_OVERLAYS`).
- Add `boat-bootmode-guard`, which runs every boot and rewrites the
  variable with efivarfs if it is not 0. The DT only seeds an absent
  variable, so existing boards and "reset to defaults" in the UEFI menu
  need this.
- Treat a boot **without `rauc.slot=`** on the cmdline as "booted outside
  GRUB". `boat-update status` reports it loudly, and no install is allowed
  in that state.
- Alternative mechanism if the overlay route misbehaves: a UEFI boot entry
  whose LoadOptions carry `bootmode=grub`, which L4TLauncher also honours.

**Fallbacks below GRUB:**

- If loading `grubaa64.efi` fails, L4TLauncher drops to DIRECT, which boots
  extlinux on `APP` (slot A), then `boot.img`.
- `UBOOT_EXTLINUX = "1"` is set because its recipe is also what puts
  `/boot/Image` and `/boot/initrd` into the rootfs for GRUB to read.
- `boot.img` is a last resort only. Its kernel is the flash-time one and
  will not match a newer slot's `/lib/modules`.
- **GRUB *starting* and then failing does not fall back.** L4TLauncher only
  falls back when the *load* fails, and GRUB disables the UEFI watchdog
  during its own init. `grub.cfg` must therefore never stop at a prompt
  (5.3).

### 5.2 Disk layout (custom external layout, `boat-nvme-ab.xml`)

This is NVIDIA's `flash_l4t_nvme_rootfs_ab.xml` with these changes:

- `APP_b` has no filename (not written at flash).
- **Fixed `<unique_guid>` PARTUUIDs** on `APP`, `APP_b`, `esp`, `SWAP` and
  `DATA`.
- `SWAP` and `DATA` are added at the end.

| # (GPT id) | name | size | written at flash? | notes |
|---|---|---|---|---|
| … | kernel, kernel-dtb, kernel_b, kernel-dtb_b, recovery, `esp`, `esp_alt`, UDA, … | as NVIDIA | as NVIDIA | unchanged, except that `esp` gains GRUB |
| 1 | `APP` | `ROOTFS_SIZE_BYTES` (16 GiB) | yes, a **smaller** ext4 image (below) | slot A |
| 2 | `APP_b` | same | **no** | slot B. `B_OK=0` until the first update writes it. |
| n | `SWAP` | 8 GiB | no | `mkswap` on first boot |
| n+1 | `DATA` | min 2 GiB, **fill-to-end** (`0x808`) | small ext4 seed image via `DATAFILE` | online `resize2fs` on first boot |

Points that need care:

- **Address partitions by PARTUUID only, never by label** (review M5). A
  JetPack SD card in the module's slot, or a USB stick, can carry
  `APP`/`esp` labels too. With the module's own SD slot, a label lookup
  could mount someone else's rootfs or write `grubenv` to the wrong ESP.
  - RAUC slots and mount units use `/dev/disk/by-partuuid/…`.
  - meta-tegra's `boot-efi.mount` (`by-partlabel/esp`) gets a drop-in.
  - The initramfs patch makes `root=PARTUUID=` authoritative and removes
    the `mmcblk0p1` fallback.
  - GRUB addresses slots as `($disk,gpt1)` / `($disk,gpt2)`, with `$disk`
    taken from the ESP's own `$root` (needs `regexp` in `GRUB_BUILDIN`;
    GRUB 2.06 has no part-label search).
- **Both slots share one ext4 UUID** (same image). Never refer to the rootfs
  by filesystem UUID. Never `tune2fs -U` a slot either: GRUB 2.06's ext2
  driver lacks `metadata_csum_seed`, which is why meta-tegra disables that
  feature.
- **Ship a smaller rootfs image.** meta-tegra sizes the ext4 to
  `ROOTFSPART_SIZE` minus a margin, so today every flash and every bundle
  install writes ~16 GiB. Set `IMAGE_ROOTFS_SIZE` explicitly (content
  plus ~2 GiB), and grow it inside the partition later:
  - RAUC's `resize=true` grows it after a bundle install;
  - `boat-storage` grows it on first boot after a flash;
  - both are online grows within a fixed partition, with no table edit.

  This makes the flash faster (only the image bytes cross USB 2.0), and
  `APP_b` adds nothing at flash. Phase 1 verifies the override sticks.
- **`TEGRA_EXTERNAL_DEVICE_SECTORS`** becomes
  `(2 × ROOTFSPART_SIZE + swap + DATA_min + 2 GiB) / 512`. The fixed
  partitions in NVIDIA's A/B XML already add up to about 1.1 GiB, so the
  current 1 GiB margin is too small. All three copies of the expression move
  together, or the margin is derived from the XML at build time.
- **Do not set `USE_REDUNDANT_FLASH_LAYOUT = "1"`.** It installs the
  `RootfsRedundancyLevel = 1` overlay (chain-coupled failover, section 3)
  and derives a template name that doesn't exist. Select the custom file
  with `PARTITION_LAYOUT_EXTERNAL` and install it from a `tegra-bootfiles`
  bbappend.
- **Slot size is fixed for the life of the layout.** 16 GiB per slot is
  about 3× today's ~5 GiB image. With Docker on `/data`,
  `IMAGE_ROOTFS_EXTRA_SPACE` (4 GiB) can go.
- **`boat-grow-rootfs` must not run on this layout.** It is removed from the
  A/B image and refuses if `APP_b` exists. `boat-storage` only ever grows
  filesystems inside their partitions; it never edits the partition table.
- **Mounts ship in the image** as mount units by PARTUUID, all `nofail`
  apart from the slot's own rootfs. A runtime edit to `/etc/fstab` would be
  lost on update. A broken *shared* resource (ESP, `DATA`, `SWAP`) must
  never drop a slot into emergency mode (5.5).

### 5.3 Slot selection: the GRUB state machine

`grubenv` on the ESP holds:

| variable | owner | meaning |
|---|---|---|
| `ORDER` | RAUC | e.g. `"B A"`; GRUB tries slots in this order |
| `A_OK` / `B_OK` | RAUC + GRUB | slot is usable. RAUC sets 0 before writing a slot; GRUB sets 0 when a slot has used up its attempts. |
| `A_TRY` / `B_TRY` | GRUB counts up, RAUC `mark-good` resets to 0 | **unconfirmed boot attempts** (a count, not a flag) |
| `A_COMMITTED` / `B_COMMITTED` | ours | 1 once that slot's image passed a trial boot. Cleared for the target by the **pre-install** handler, so a fresh slot is a trial *by default* (review M7). |
| `BOAT_STAY` | ours | bench escape hatch: GRUB adds `boat.stay=1` (no automatic reboots, 5.5) |

GRUB logic (derived from RAUC's `contrib/grub.conf`, with our counter added):

1. `timeout=0`, no menu, GRUB superuser password (no editing at the
   console).
2. `load_env`. If `grubenv` is unreadable, use built-in defaults:
   `ORDER="A B"`, `A_OK=1`, `A_COMMITTED=1`, `B_OK=0`. In this state
   `save_env` is impossible, so the guard unit (below) repairs the file on
   the next boot.
3. For each slot in `ORDER`, skip it if `OK≠1`.
   - The limit is **2** if `COMMITTED=1`, else **1**.
   - If `TRY ≥ limit`, set `OK=0` and go to the next slot.
   - Otherwise set `TRY=TRY+1`, `save_env`, and boot the slot (add
     `boat.trial=1` when not committed).
4. **Every** load step is checked:
   `if ! linux …; then <slot>_OK=0; save_env; reboot; fi`. The same goes
   for `initrd`. GRUB never stops at a prompt.
5. If no slot qualifies, boot `A` without counting, with `boat.lastresort=1`.
   If that also fails, the end state is NVIDIA's sticky recovery (below).
6. The last line is `reboot`.

At build time, `grub.cfg` is generated from `KERNEL_ARGS` and checked with
`grub-script-check`. The bundle recipe also fails if the rootfs lacks
`/boot/Image` or `/boot/initrd`.

**Why committed slots get 2 attempts and trials get 1** (review M1):

- A committed system survives **one** power cut or brownout during boot
  without flipping to the older slot. A slot that keeps failing still
  hands over, on the third boot.
- The handover must happen **before NVIDIA's counter trips.** At
  `RootfsRedundancyLevel 0`, L4TLauncher decrements a 2-bit count (max 3)
  in a PMC scratch register on every warm boot of "slot A".
  `nv_update_verifier` resets it once userspace is up. A 4th consecutive
  warm boot that never got there makes L4TLauncher write the
  **non-volatile** `RootfsStatusSlotA = unbootable`. From then on every
  boot, cold ones included, goes to the (empty) recovery partition: the
  board is stuck in UEFI until someone resets that variable in the UEFI
  menu or reflashes.
- With limits 2 and 1, the boot sequence on a slot that keeps failing is:
  A, A, then B. So B gets the 3rd boot, which is still within NVIDIA's
  budget.

| scenario | boots | outcome |
|---|---|---|
| trial B panics | B(1 try) → A | A commits; B `OK=0`; NVIDIA count reset on A |
| committed A, one power cut mid-boot | A(TRY 1) → cold boot → A(TRY 2) → mark-good | stays on A (cold boot also re-seeds NVIDIA's register; Phase 0 confirms this) |
| committed A broken, B good | A → A → B | B runs; reported in `status` |
| both broken | A → A → B → (4th warm boot) | NVIDIA sticky recovery → stops in UEFI. The recovery procedure is documented (UEFI menu path captured in Phase 0, or USB reflash). |

**`grubenv` integrity** (review M2):

- RAUC rewrites `grubenv` with `grub-editenv`, which truncates and then
  writes. On vfat that is not atomic, and RAUC fails outright if a variable
  is missing.
- `boat-grubenv-guard.service` runs early on every boot, after the ESP
  mount and before `boat-commit`. It validates the file and keeps a
  last-good copy, `grubenv.bak`.
- If the file is invalid, the guard rebuilds it. The booted slot (from
  `rauc.slot`) becomes `OK=1, COMMITTED=1`. The other slot is taken from
  RAUC's central status on `/data`, or `OK=0` if that is unknown. The guard
  alerts.
- `grub.cfg` falls back to `grubenv.bak` for *reading* if `grubenv` won't
  load.

### 5.4 What "booted correctly" means

There are two tiers, so that a broken *data* partition or an app problem can
never flip the OS back to an older slot.

- **Tier 1 (every boot):**
  - the kernel booted the slot named by `rauc.slot`, from the PARTUUID that
    slot is configured for;
  - the rootfs is read-write;
  - the ESP is mounted;
  - `grubenv` is valid;
  - `rauc status` works.
- **Tier 2 (trial boots only, `boat.trial=1`):**
  - critical units are active: `NetworkManager`, `sshd`/`sshd.socket`,
    `systemd-logind`, `dbus`, `boat-bootmode-guard`, and `docker` if `/data`
    is mounted;
  - no failed units in a configurable critical list;
  - optional operator checks in `/data/boat-update/health.d/*`, each with a
    timeout, **off by default**.
- **Never part of either tier:** anything external (internet, NTP, GNSS,
  registry pulls, NMEA).

Mechanics (review M3: meta-rauc's `rauc-mark-good.service` is
`WantedBy=multi-user.target`, which orders `multi-user.target` *after* it, so
a "multi-user reached" check would deadlock):

- Mask meta-rauc's `rauc-mark-good.service`. One unit does the gating and
  the marking: `boat-commit.service`. It is `Type=oneshot` and
  `DefaultDependencies=no` (so no target orders after it), `After=` the
  critical units, `WantedBy=multi-user.target`, and retries checks for up to
  `TimeoutStartSec=10min`.
- On success: `rauc status mark-good` (TRY=0), then `<slot>_COMMITTED=1`.
  Either order is safe under a power cut, because the next boot just
  re-checks.
- **Trial failure:** `rauc status mark-bad booted`, record the reason in
  `/data/boat-update/last-result` (best effort), then reboot. GRUB skips the
  slot.
- **Committed Tier 1 failure:** no mark, no reboot. An alert shows in
  `status`. The slot keeps running; the GRUB counter only hands over if it
  then *also* fails to boot.
- `boat-trial-deadline.timer` (`ConditionKernelCommandLine=boat.trial=1`,
  `OnBootSec=15min`): if the trial still isn't committed, mark-bad and
  reboot. This catches a commit unit that hangs.
- **The sleep inhibitor** is held from install until the trial is committed,
  not just during the write.
- `nv_update_verifier.service` keeps running unconditionally. It only feeds
  NVIDIA's counter. Phase 1 confirms that it also resets that counter while
  booted from `APP_b` (≥4 warm reboots on B).

### 5.5 Making every failure end in a reset

A failover only happens on the *next* boot, so every way of failing must end
in a reset. The exception is a deliberate stay on a committed slot.

| failure | what turns it into a reset | change needed |
|---|---|---|
| firmware fails before UEFI | not covered (single chain); firmware never changes in the field | none |
| `grubaa64.efi` missing/unloadable | L4TLauncher falls back to DIRECT → extlinux on `APP` (A) → `boot.img` | none; `status` flags "booted outside GRUB" |
| `grub.cfg` / kernel / initrd load error | guarded loads → mark slot, `reboot` (5.3) | grub.cfg design + build checks |
| GRUB itself hangs | **only the hardware WDT, if it runs during UEFI/GRUB** (GRUB disables the UEFI watchdog) | Phase 0 question; otherwise documented as power-cycle-to-recover |
| kernel panic | `panic=10` | cmdline |
| kernel hard hang | Tegra WDT running from early kernel | Phase 0: armed before userspace? If not, enable it at probe |
| initramfs can't mount root | today: **hangs forever** | `tegra-minimal-init` bbappend: `root=PARTUUID` only, bounded retries, then `reboot -f` (`boat.stay=1` keeps the shell) |
| systemd emergency/rescue | today: waits forever | drop-ins: reboot after 60 s unless `boat.stay=1`. Shared mounts are `nofail`, so only slot-local faults get here. |
| PID 1 wedged | `RuntimeWatchdogSec=60s`, `RebootWatchdogSec=10min` | **decision D8**: one `/dev/watchdog` opener |
| commit/health hangs on a trial | `boat-trial-deadline.timer` | add |
| power cut during a trial | TRY already counted → next boot skips the trial slot | none (conservative) |

The `boat.stay=1` escape hatch comes from the **cmdline** (GRUB, from
`BOAT_STAY`), not from a file on `/data`. `/data` may not be mounted in
emergency mode.

### 5.6 The install sequence, and what a power cut does at each step

`boat-update install <bundle>` is a thin wrapper around `rauc install`:

- It refuses unless booted via GRUB (`rauc.slot` present) and `/data` is a
  real mountpoint. Otherwise RAUC would `mkdir` its data directory on the
  rootfs.
- It takes the sleep + shutdown inhibitor until commit (5.4).
- `boat-sleep --force` also checks for an update in progress.

The order matches RAUC 1.13's `install.c`:

| step | who | if power is lost here |
|---|---|---|
| 1. verify signature, verity format, `compatible`, version | RAUC (`min-bundle-version`, see 5.7) | nothing changed |
| 2. system `pre-install` handler: BSP/L4T and Docker-version checks from the bundle's `[meta.boat]` manifest section; set target `COMMITTED=0` | ours | target merely marked not-committed |
| 3. set target `OK=0` | RAUC grub backend | target unbootable; stay on current |
| 4. write image into the target slot, `resize=true` | RAUC (`type=ext4`) | target half-written, `OK=0` |
| 5. slot `post-install` hook: carry-over into the new slot (5.8) | ours | target still `OK=0` |
| 6. activate: `ORDER=target first`, `OK=1`, `TRY=0` | RAUC | new slot boots **as a trial** (COMMITTED=0 since step 2). If `grubenv` is torn, see 5.3 integrity. |
| 7. system `post-install` handler: append history on `/data` | ours, best effort | none |
| 8. reboot (operator or `--reboot`) → trial → commit or fallback | 5.3 / 5.4 | TRY counted → fallback |

- **No post-write read-back hash.** RAUC resizes the filesystem before slot
  hooks run, so a hash taken in the hook would never match.
- **Manual rollback:** `boat-update rollback` runs
  `rauc status mark-active other` and reboots. It refuses if the other slot
  is `OK=0`. It is a slot switch; nothing is reinstalled.
- **Transport:** an SSH push to `/data` and then a local install, or a USB
  stick. A bundle of several GB exceeds FAT32's 4 GiB file limit, so the
  stick must be exFAT or ext4. `install <URL>` (RAUC streaming over nbd) is
  deferred to Phase 7.

### 5.7 Security model

- **Bundle authenticity:**
  - RAUC CMS signatures (X.509), with keyring `/etc/rauc/ca.cert.pem` in
    each slot.
  - `bundle-formats=-plain`, so only **verity** bundles are accepted.
    dm-verity protects the payload after the signature check, closing the
    check-then-use gap.
  - `check-purpose=codesign`, with a signing certificate that carries the
    codeSigning EKU.
- **Who may drive RAUC** (review M6). Upstream's
  `de.pengutronix.rauc.conf` *allows anyone* to call RAUC's D-Bus API,
  including `Install` with `ignore-compatible` and `Mark`, and Signal K's
  compose example mounts the system bus into a container. Ship a
  **root-only** policy (bbappend) and test it from an unprivileged user and
  from a container.
- **Clock.** There is no RTC, so the clock may read 1970.
  `use-bundle-signing-time=true` makes RAUC check certificates at the
  bundle's signed signing time. The trade-off:
  - certificate *expiry* stops meaning anything, because a leaked key can
    backdate;
  - revocation therefore means removing the CA from the keyring (or a CRL)
    in **both** slots;
  - after a key rotation, a rollback lands on a slot that trusts only the
    old CA. Keep the old CA in the keyring until both slots carry the new
    keyring.
- **Keys** (review M9):
  - `scripts/gen-update-keys.sh` writes to `BOAT_KEYS_DIR`, outside the repo
    *and* outside `WORKROOT`. The default is
    `~/.local/share/boat-update-keys`, because `yocto/` gets `rm -rf`'d and
    losing the CA means a USB reflash of every board.
  - The script refuses to overwrite, uses mode 600, and prints a backup
    instruction.
  - `local.conf` points `RAUC_KEY_FILE`, `RAUC_CERT_FILE` and
    `RAUC_KEYRING_FILE` there, and **the build fails if they are missing**.
    meta-rauc's placeholder CA is a comment-only dummy, so it fails closed,
    but the error should name our variable.
  - `lint.sh` fails if any tracked file contains `PRIVATE KEY-----`.
- **Fault-injection bundles** (section 7) are signed by a **separate bench
  CA**, trusted only by images built with `BOAT_BENCH=1`. A boat image
  never trusts it.
- **Production:** the CA private key stays offline, and the signing key can
  live on a PKCS#11 token.
- **Compatibility and downgrade:** `compatible=boat-xavier-nx` rejects
  bundles for other boards. RAUC's `min-bundle-version` (D6) covers
  downgrades. Falling back after a failed update is a slot switch, not an
  install, so the version policy never blocks a failover.
- **Attack surface:** no update daemon and no new port. Installs start over
  the existing SSH or from local media.
- **Honest limits:**
  - This authenticates *updates*. Root on the running board can still write
    anything to disk, and the bench image's shared password (README
    "Credentials") still applies.
  - A physical attacker with USB recovery mode can reflash.
  - Secure Boot and dm-verity (Phase 7) close those gaps. For option A that
    also means signing GRUB and having it verify the kernel.

### 5.8 State that must survive a slot switch

`/etc` and `/var` belong to the slot, and an update replaces them.

- **Carry-over at install** (the slot post-install hook copies these from
  the running slot into the new one; early-boot safe, and each slot stays
  self-contained for rollback):
  - `/etc/machine-id`;
  - `/etc/ssh/ssh_host_*`;
  - `/root/.ssh/` (authorized keys: docs/05 recommends key-only SSH, so
    losing them locks you out, review M8);
  - `/etc/boat-sleep.key`;
  - `/etc/NetworkManager/system-connections/`;
  - `/var/lib/bluetooth/`;
  - `/etc/hostname` and `/etc/localtime`;
  - the operator-edited `CONFFILES` (`/etc/default/boat-power`,
    `/etc/default/boat-bluetooth`, `/etc/docker/daemon.json`);
  - the password hash fields of `root` and `boat`, merged (**D5**);
  - `/etc/nv_boot_control.conf`.
- **Lives on `/data`:**
  - Docker and compose (already there);
  - RAUC's `data-directory` (`/data/rauc`, with `RequiresMountsFor=/data`
    on `rauc.service`);
  - `boat-update` history;
  - a size-capped persistent journal (`/data/log/journal`), so
    `journalctl -b -1` after a fallback shows why the trial failed;
  - **`/home`** (D11, recommended). This keeps `~boat/.ssh`, the XFCE
    settings and anything users keep in their home across updates. It is
    seeded from the image's skeleton on first boot.
- Documented consequence: local edits to any other `/etc` file do not
  survive an update. Put them in `meta-boat` or on `/data`.
- `/data` is shared, so a newer Docker could migrate `/data/docker` in a way
  an older slot can't read. The `pre-install` handler warns when the Docker
  major version differs.

### 5.9 What stays outside A/B, deliberately

| what | why | how it changes |
|---|---|---|
| QSPI firmware (one usable chain) | section 3 | USB recovery (`05-flash-nvme.sh`) |
| kernel DTB (`kernel-dtb`, read by UEFI) | option A trade-off | USB reflash. Phase 0 confirms GRUB passes UEFI's DTB through unchanged. |
| ESP contents: L4TLauncher, GRUB, `grub.cfg` | single copy | USB reflash (`grubenv` and `boot.cfg` are runtime-written) |
| `kernel`/`kernel_b` (`boot.img`) | last-resort fallback only | flash time |

A BSP bump (new L4T) is therefore always a **full USB reflash**. The
`pre-install` handler rejects a bundle whose `[meta.boat] l4t=` differs from
the running `/etc/nv_tegra_release`.

---

## 6. Implementation phases

Each phase ends with something testable on the bench. Nothing ships to the
boat before Phase 6 passes.

**Phase 0: hardware spike (bench board, USB recovery at hand, no repo changes).**
A "no" on any of these re-opens section 4.

1. **Capture the current state:**
   - `/proc/cmdline`, `ls /boot`, `cat /proc/mtd`;
   - `nvbootctrl dump-slots-info`, `sgdisk -p /dev/nvme0n1`;
   - ESP free space, and `efivar -l` (is `L4TDefaultBootMode` present, and
     what is its value?);
   - kernel config for `DM_VERITY|BLK_DEV_LOOP|SQUASHFS(_XZ)|BLK_DEV_NBD`.
2. **GRUB hand-off on the running board** (no reflash):
   - set `L4TDefaultBootMode=0` with efivarfs;
   - copy a hand-built `grubaa64.efi` + `grub.cfg` to
     `/boot/efi/EFI/BOOT/`;
   - boot `/boot/Image` + `/boot/initrd` (copied in by hand) with
     `${cbootargs}`.

   Then check:
   - HDMI/XFCE, Wi-Fi/BT, GPU (`docker run --runtime nvidia`), fan/nvpmodel;
   - **SC7 sleep + WoL wake**;
   - `/proc/device-tree` is identical to a normal boot.

   Revert by setting the variable back to 1.
3. **Watchdogs:**
   - Is the Tegra WDT running during UEFI/GRUB? Test with a `grub.cfg` that
     just `sleep`s forever.
   - Is it running before userspace (`/sys/class/watchdog/watchdog0/state`
     early in boot)?
   - With `panic=10`, does `echo c > /proc/sysrq-trigger` reset the board?
     Does a hard hang?
4. **NVIDIA's counter:**
   - a cold power-on re-seeds the scratch register;
   - capture the UEFI-menu path that resets `RootfsStatusSlotA`, for the
     sticky-recovery procedure.
5. *(Optional, bench only.)* Confirm section 3 by switching to firmware
   chain B and watching it fail and revert.

**Phase 1: layout, GRUB, slot-aware boot.** Effort: M.

- **Toggle.** `AB_LAYOUT` in `env.sh` (default **off** until Phase 6). For
  kas, a separate overlay `kas/ab-updates.yml` (static YAML can't read
  env.sh), plus an A/B block in `config/local.conf.sample`.
- **Build configuration** (`02-configure-build.sh` and the kas overlay):
  - `PARTITION_LAYOUT_EXTERNAL`;
  - the new sector expression;
  - `UBOOT_EXTLINUX = "1"`;
  - `IMAGE_ROOTFS_SIZE`;
  - `IMAGE_TEGRAFLASH_DATA`/`DATAFILE`.
- **`meta-boat` additions:**
  - a `tegra-bootfiles` bbappend for `boat-nvme-ab.xml` (fixed PARTUUIDs);
  - the `L4TConfiguration` overlay with GRUB mode;
  - `boat-bootmode-guard`;
  - `boat-grub-esp`: `grubaa64.efi` with `GRUB_BUILDIN` +=
    `regexp loadenv test echo sleep reboot halt`, `grub.cfg` (generated and
    script-checked), and a pre-sized 1 KiB `grubenv`;
  - a `tegra-espimage` bbappend;
  - a `tegra-minimal-init` bbappend;
  - `grub-editenv` on the target;
  - `boat-grubenv-guard`;
  - **a stub commit unit** that runs `grub-editenv … TRY=0 COMMITTED=1`.
    Without it, slot A would be marked bad on its second boot (review M10).
  - kernel fragment `boat-rauc.cfg` for whatever Phase 0 shows missing.
- **Exit criteria:**
  - a fresh flash boots A through GRUB;
  - a test image `dd`'d into B boots via `grub-editenv`;
  - `sysrq-c` during a B trial lands on A;
  - **≥4 consecutive warm reboots on B** do not trip NVIDIA's counter;
  - removing `grubaa64.efi` still boots A via DIRECT.

**Phase 2: storage and state.** Effort: S–M.

- **`boat-storage`** (first boot, idempotent):
  - grows `DATA` and the flashed `APP` ext4 online;
  - runs `mkswap` on `SWAP` unless it already holds swap;
  - acts on PARTUUIDs only, so stale data under `SWAP` from an older layout
    is fine.
- **Mount units**, `nofail`:
  - `DATA` → `/data`;
  - `SWAP`;
  - the bind mounts `/home` and `/var/log/journal`.
- **The ESP mount drop-in** (PARTUUID; verify its failure doesn't drop to
  emergency).
- **`boat-grow-rootfs`:** refuses on A/B layouts and is dropped from the A/B
  image.
- **Exit:**
  - the first boot after a flash has `/data` filling the SSD, 8 GiB of
    swap, and `APP`'s filesystem filling its partition;
  - a reboot changes nothing;
  - pulling the `DATA` partition (deleting the GPT entry on a bench unit)
    still boots to a working desktop.

**Phase 3: RAUC.** Effort: M.

- **Layer plumbing:**
  - `01-fetch-layers.sh` + kas: add `meta-rauc` (kirkstone ships RAUC
    1.13);
  - bblayers in all three places, `LAYERRECOMMENDS_boat += "rauc"`;
  - appends under `layers/meta-boat/external/rauc/`;
  - `DISTRO_FEATURES += "rauc"`.
- **`rauc` bbappend:**
  - `system.conf`: `bootloader=grub`, `grubenv=` on the ESP, slots
    `rootfs.0`/`.1` by PARTUUID with `bootname=A`/`B`, `type=ext4`,
    `resize=true`, `data-directory=/data/rauc`, keyring options from 5.7,
    `min-bundle-version`;
  - the root-only D-Bus policy;
  - a `rauc.service` drop-in.
- **Keys and bundle:**
  - `scripts/gen-update-keys.sh`;
  - `boat-bundle.bb` (`inherit bundle`, `RAUC_BUNDLE_FORMAT = "verity"`, a
    `[meta.boat]` section with `l4t` and `docker`, and a check that
    `/boot/Image` and `/boot/initrd` exist).
- **Handlers:**
  - system `pre-install` (BSP/Docker checks, `COMMITTED=0` for the target);
  - slot `post-install` (carry-over);
  - system `post-install` (history).
- **Exit:**
  - `rauc install` writes B, the reboot is a trial, and it commits;
  - `rauc status` is right on both slots;
  - an unprivileged `busctl call` to RAUC is denied.

**Phase 4: health gate and reset coverage.** Effort: M.

- `boat-commit` (Tier 1/2, configured in `/etc/boat-update/health.conf`),
  replacing the Phase 1 stub.
- `boat-trial-deadline.timer`.
- Emergency/rescue drop-ins.
- Watchdog consolidation (D8), re-tested across SC7 because it touches
  `boat-power`'s sleep hook.
- The inhibitor held until commit.
- **Exit:** every row of 5.5 except firmware has been exercised once.

**Phase 5: operator tooling and docs.** Effort: S–M.

- **`boat-update`.** `status` is the default and read-only. It shows:
  - the booted slot and whether it is a trial or committed;
  - the last result and its reason;
  - both slots' versions;
  - "booted outside GRUB" and `grubenv` alerts.

  Plus `install [--reboot]`, `rollback`, `history`, `debug-hold` (sets
  `BOAT_STAY`).
- **`scripts/push-update.sh <host> [bundle]`:** copy to `/data`, install,
  reboot, then *wait and report which slot came back*, in the style of
  `wol/boat-wake.sh`.
- **`docs/07-updates.md`**, plus fixes to:
  - docs/01 (boot flow) and docs/04 (layout, flash; the first A/B flash
    erases the SSD);
  - docs/04 `--host-drive`: `doexternal.sh` isn't shipped by meta-tegra
    kirkstone, so A/B is `initrd-flash` only;
  - docs/05 "Updates" and "Reclaiming the rest of the SSD";
  - docs/06 troubleshooting, including the sticky-recovery procedure;
  - README.

**Phase 6: validation.** Effort: M, mostly bench time.

- Run the section 7 matrix.
- Soak: 50 update cycles alternating good and faulty bundles, plus random
  power cuts if a USB relay on the barrel jack is available.
- Then flip `AB_LAYOUT` to default-on.

**Phase 7 (later, separate plans):**

- HTTPS streaming + RAUC adaptive updates, so only changed blocks cross a
  cellular link;
- Secure Boot, dm-verity and a read-only rootfs;
- credential lockdown;
- option B on an eMMC module.

---

## 7. Test plan: the acceptance matrix

All tests run on hardware, because QEMU cannot emulate Tegra UEFI. Two
pieces also run off-target:

- **GRUB state machine:** host tests under `qemu-system-aarch64` + AAVMF
  (generic UEFI). They boot a dummy kernel and assert the `grubenv`
  transitions for every row of 5.3's table.
- **Shell logic:** `boat-update`, the handlers and `boat-storage` get
  host-side tests with `rauc`, `grub-editenv` and `blkid` mocked.

Both run from `lint.sh`, so they cover every PR.

| # | scenario | expected |
|---|---|---|
| 1 | good bundle, normal reboot | trial → Tier 2 passes → committed |
| 2 | bundle signed by an unknown key / by the bench CA on a boat image | rejected before anything is written |
| 3 | `plain` bundle | rejected |
| 4 | wrong `compatible` | rejected |
| 5 | older version (below `min-bundle-version`) | rejected |
| 6 | bundle for a different L4T | rejected |
| 7 | clock at 1970, valid bundle | installs |
| 8 | power cut during the slot write | old slot; target `OK=0`; re-install works |
| 9 | power cut between activate and reboot, and between activate and the post-install handler | next boot is a **trial** of the new slot (`COMMITTED=0` since pre-install) |
| 10 | power cut during a trial boot | old slot; new slot `OK=0`; `status` says so |
| 11 | **one** power cut during boot of a *committed* slot | stays on that slot |
| 12 | committed slot broken (bench: corrupt its kernel) | A, A, then B; no NVIDIA sticky recovery |
| 13 | trial: kernel panic | `panic=10` → old slot |
| 14 | trial: rootfs won't mount | initramfs reboots → old slot |
| 15 | trial: emergency mode | reboot after 60 s → old slot |
| 16 | trial: Tier 2 failure (`docker` masked) | mark-bad + reboot → old slot, reason recorded |
| 17 | trial: hard hang | WDT reset → old slot |
| 18 | trial: commit unit hangs | deadline timer → old slot |
| 19 | bundle whose rootfs lacks `/boot/Image` | rejected at bundle build; if forced, GRUB marks the slot and reboots |
| 20 | committed slot, `/data` missing | stays up (Tier 1 ok); Docker degraded; alert |
| 21 | ESP `grubenv` corrupted | boots by defaults; the guard rebuilds the file and alerts; next install works |
| 22 | `L4TDefaultBootMode` reset to 1 (UEFI defaults) | the guard restores it; that boot is flagged "outside GRUB"; install refused until rebooted via GRUB |
| 23 | `grubaa64.efi` missing | DIRECT boots A; flagged |
| 24 | a JetPack SD card in the module slot, and a USB stick with `APP`/`esp` labels | ignored: root, ESP and RAUC slots resolve to NVMe PARTUUIDs |
| 25 | unprivileged user, and a container with the system bus mounted, call RAUC `Install`/`Mark` | denied |
| 26 | `boat-update rollback` after commit | other slot boots and commits |
| 27 | `boat-sleep` (and the UDP sleep listener) during install and during a trial | refused |
| 28 | both slots broken | A, A, B, then UEFI stop (sticky recovery) is the documented end state; the documented recovery works |
| 29 | carry-over | SSH host key, `/root/.ssh`, `/home`, Wi-Fi, BT pairing, hostname, `boat-sleep.key`, passwords (D5) are unchanged after update and after rollback |
| 30 | SC7 + WoL on both slots | works |

The fault bundles for 12–19 come from a `boat-image-faulty` variant
(`BOAT_FAULT=<kind>`), signed by the bench CA (5.7).

---

## 8. Migrating existing boards

The layout changes, and `APP` currently spans the SSD, so **the first A/B
install is a one-time full USB reflash that erases the SSD**. There is no
safe in-place conversion, and the plan does not attempt one.

1. On the old system, back up `/data` (compose, volumes, Signal K state),
   `/home`, `/root/.ssh` and any hand-edited `/etc` files to a laptop with
   `rsync`.
2. Full flash with `AB_LAYOUT=1`. The ESP lives in the external layout, so
   even `--skip-bootloader` rewrites it. A full flash is still recommended
   for the first A/B install, so that the GRUB-mode `L4TConfiguration`
   overlay lands in QSPI. (`boat-bootmode-guard` would fix the variable at
   runtime regardless.)
3. First boot: `boat-storage` provisions `/data`, swap and the rootfs size.
   Restore the backup.
4. Install a bundle once (A→B) and once more (B→A) to prove the round trip
   before the boat leaves the dock.

---

## 9. Risks and open questions

| risk | likelihood / impact | mitigation |
|---|---|---|
| GRUB on this UEFI mishandles the DTB, initrd or cmdline (SC7, display) | M / H | Phase 0 step 2 first; option C is the fallback |
| Tegra WDT not running in GRUB or early kernel | M / M | Phase 0 step 3; enable at probe; otherwise document power-cycle-to-recover |
| NVIDIA sticky recovery reached (both slots fail, or a shared fault that slipped past `nofail`) | L / H | the 2+1 attempt budget (5.3); `nofail` shared mounts; documented UEFI-menu reset; test 28 |
| `nvbootctrl verify` doesn't reset the count when booted from `APP_b` | M / H | Phase 1 exit criterion; if it fails, `boat-commit` resets it another way, or the budget becomes 1+1 |
| `grubenv` torn on vfat | L / M | guard unit, `.bak`, defaults (5.3) |
| ESP (vfat) damaged | L / H | rare, tiny writes; `fsck.fat` reported in `status`; recovery = USB reflash |
| a shared `/data` written by a newer slot breaks an older one | L / M | Docker-version warning; Docker stays on the kirkstone major |
| bundle size over cellular (several GB with CUDA) | H / M | shore Wi-Fi now; adaptive streaming in Phase 7 |
| meta-rauc vs this repo's host workarounds (Python 3.14, gcc-12) | M / L | build `rauc-native` first in Phase 3 |
| `IMAGE_ROOTFS_SIZE` override ignored by meta-tegra's sizing | M / L | Phase 1 check; otherwise accept 16 GiB writes |

---

## 10. Decisions needed before coding

| # | question | recommendation |
|---|---|---|
| D1 | Architecture: A (GRUB hand-off, current hardware), B (eMMC module, NVIDIA-native incl. firmware OTA), or C (patch L4TLauncher)? | **A** |
| D2 | Slot size | **16 GiB** per slot (with a smaller ext4 image inside) |
| D3 | Firmware and DTB stay USB-reflash-only | **yes** |
| D4 | One-time erase-and-reflash migration | **yes**, with the section 8 backup |
| D5 | Carry passwords over, or reset them to the image's `BOAT_PASSWORD_HASH`? | **carry over** (merge the hash only) |
| D6 | Downgrades: reject via `min-bundle-version`, raised deliberately per release? | **yes** |
| D7 | Transport for now | SSH push + USB stick (exFAT/ext4); HTTPS streaming later |
| D8 | Watchdog: systemd `RuntimeWatchdogSec` as the only `/dev/watchdog` opener? | **yes** (the `watchdog` daemon's custom checks aren't used) |
| D9 | Key custody: dev keys outside `WORKROOT` with backup; production CA offline; bench CA separate | **yes** |
| D10 | Attempt budget: trial 1, committed 2 | **yes** (bounded by NVIDIA's 3-warm-boot limit) |
| D11 | `/home` on `/data` (persistent across updates) vs carry-over of `~/.ssh` only | **`/home` on `/data`** |

---

## 11. Review log

### Review pass 1 (independent adversarial review of v1)

Every finding below was checked against the upstream source before v2 was
written.

| id | severity | finding | resolution in v2 |
|---|---|---|---|
| B1 | blocker | L4TLauncher's default mode is DIRECT (`L4TDefaultBootMode = 1` in NVIDIA's `L4TConfiguration.dts`). GRUB would never load; with `UBOOT_EXTLINUX=1` the board would always boot `APP`. | GRUB-mode overlay + `boat-bootmode-guard` + "booted outside GRUB" detection (5.1); Phase 0 step 2 sets the variable first |
| B2 | blocker | A GRUB-stage failure doesn't fall back: GRUB disables the UEFI watchdog and drops to a prompt on boot failure; L4TLauncher only falls back on *load* failure; `regexp` isn't in poky's `GRUB_BUILDIN` | `timeout=0`, guarded loads, final `reboot`, module list, `grub-script-check`, bundle `/boot` check (5.3); WDT-in-GRUB question in Phase 0 |
| M1 | major | RAUC's template arms TRY on *every* boot, so one power cut on a committed slot would flip the boat to the older OS | TRY becomes an attempt count; committed limit 2, trial 1, bounded by NVIDIA's counter (5.3); tests 11–12 |
| M2 | major | A torn `grubenv` is not "one wasted boot": `grub-editenv` truncates then writes, `save_env` refuses a bad file, RAUC fails on missing variables | `boat-grubenv-guard`, `.bak`, defaults (5.3); test 21 |
| M3 | major | meta-rauc's mark-good is `WantedBy=multi-user.target`, so a "multi-user reached" check deadlocks; Tier 1 failure behaviour was undefined | single `boat-commit` unit with `DefaultDependencies=no`; trial fails → reboot, committed fails → alert only (5.4) |
| M4 | major | NVIDIA's counter trip is sticky (non-volatile) and stops the board in UEFI; shared faults (ESP mount) can cause it | attempt budget, `nofail` shared mounts, emergency reboot only for slot-local faults, documented recovery; Phase 1 ≥4-reboot test; test 28 |
| M5 | major | Partition labels collide with JetPack SD cards and USB media (initramfs `blkid -l`, `boot-efi.mount`, RAUC) | fixed PARTUUIDs everywhere; initramfs uses `root=PARTUUID` only and drops the `mmcblk0p1` fallback (5.2); test 24 |
| M6 | major | RAUC's stock D-Bus policy lets anyone install or mark slots, and containers get the system bus | root-only policy (5.7); test 25 |
| M7 | major | The trial marker was written after RAUC activates the slot | inverted: `COMMITTED=0` is set in **pre-install**, so a fresh slot is a trial by default (5.3, 5.6); test 9 |
| M8 | major | Carry-over missed `/root/.ssh` and `/home`, so key-only SSH would lock you out after an update | `/root/.ssh` carried; `/home` on `/data` (D11) |
| M9 | major | Keys inside `WORKROOT` get deleted; fault bundles were signed with the boat-trusted key | `BOAT_KEYS_DIR` outside `WORKROOT`, no-overwrite, backup; separate bench CA (5.7) |
| M10 | major | Phase 1 tested the TRY logic before anything could clear it | stub commit unit + `grub-editenv` in Phase 1 |
| — | minor | read-back hash can't match after `resize`; ext4 image is already 16 GiB; sector margin too small; signing-time/rotation caveats; `--skip-bootloader` reasoning wrong; handlers see only bundle metadata; RAUC `mkdir`s its data dir on the rootfs; inhibitor ends too early; escape hatch on `/data`; `AB_LAYOUT` can't reach kas; FAT32 4 GiB; URL install implies streaming; `boot.img` kernel/module mismatch; GRUB logic testable in QEMU; never `tune2fs -U` | all addressed in 5.2, 5.4, 5.6, 5.7, 5.9, 6, 7, 8 |
| — | nit | flash-time wording contradicted itself; docs/04 has the same kernel-in-APP claim; `doexternal.sh` not shipped; §5.3 wasn't RAUC's stock template | fixed / added to Phase 5 docs list |

Claims the review checked and confirmed:

- **Firmware layouts:** chain B is incomplete in the QSPI-only layout; the
  `_rootfs_ab` QSPI template is missing; the redundant layout adds the
  level-1 overlay; the capsule image list.
- **NVIDIA rootfs counter:** the level-0/1 counter behaviour and the
  firmware-switch + `ResetCold` path.
- **L4TLauncher:** the GRUB path, the `boot.cfg` format and the 5-minute
  watchdog before `StartImage`.
- **meta-tegra:** `UBOOT_EXTLINUX ?= ""`; `l4t-launcher-extlinux` installs
  `/boot/Image` and `/boot/initrd`; the initramfs slot-suffix logic and its
  unbounded loop; `nv_update_verifier` runs unconditionally.
- **RAUC 1.13:** booted-slot detection order and the mounted-slot refusal;
  the grub-backend write semantics; mark-bad before writing and activation
  after all slots; `use-bundle-signing-time` and `check-purpose`.
- **Flashing:** `make-sdcard` final-partition logic; `DATAFILE` plumbing
  through `initrd-flash`.
- **GRUB 2.06 and systemd 250:** no part-label search in GRUB;
  `boot-complete.target` and `JobTimeoutAction` exist in systemd 250.

### Your review

*(Answers to section 10, and anything else, go here before Phase 0 starts.)*

---

## Appendix: evidence (upstream sources this plan was checked against)

Revisions used:

- meta-tegra kirkstone `47519b9` (L4T R35.6.4)
- edk2-nvidia `r35.6.4-updates` `942f38f`
- meta-rauc kirkstone `4cb7c47` (RAUC 1.13)
- GRUB 2.06
- meta-mender-community kirkstone

Sources:

- **Firmware chain-B placement:** `Linux_for_Tegra/bootloader/t186ref/cfg/`
  - `flash_l4t_t194_qspi_p3668.xml` (no `secure-os_b`, `bpmp-fw_b`, …);
  - `flash_l4t_t194_spi_sd_p3668.xml` (on `sdcard`);
  - `flash_l4t_t194_spi_emmc_p3668_rootfs_ab.xml` (on `sdmmc_user`);
  - no `flash_l4t_t194_qspi_p3668_rootfs_ab.xml` exists.
- **NVMe A/B layout:**
  `Linux_for_Tegra/tools/kernel_flash/flash_l4t_nvme_rootfs_ab.xml`.
  `APP` is id 1 and `APP_b` id 2, with no fill-to-end. `make-sdcard.sh`
  `find_finalpart` disables the last-partition fill when `APP_b` exists and
  honours the first `0x808` partition.
- **Boot mode default:** edk2-nvidia
  `Silicon/NVIDIA/Tegra/DeviceTree/L4TConfiguration.dts`
  (`L4TDefaultBootMode = [01 00 00 00]`, `RootfsRetryCountMax = 3`,
  `RootfsRedundancyLevel = 0`) and
  `Silicon/NVIDIA/Include/NVIDIAConfiguration.h` (GRUB=0, DIRECT=1).
- **Rootfs/firmware coupling and the sticky unbootable state:**
  `Silicon/NVIDIA/Application/L4TLauncher/L4TRootfsValidation.{c,h}`.
- **L4TLauncher boot modes, GRUB path, `boot.cfg`, watchdog:**
  `L4TLauncher.c` (`ProcessBootParams`, mode dispatch,
  `SetWatchdogTimer`) and `L4TLauncher.h`.
- **Firmware chain switching:**
  `Silicon/NVIDIA/Drivers/BootChainDxe/BootChainDxe.c`.
- **Capsule images:** `Silicon/NVIDIA/Library/FmpDeviceLib/FwImageNames.c`.
  Default capsule signing uses EDK2 *test* certs (meta-tegra
  `classes/tegra-uefi-capsule-signing.bbclass`).
- **meta-tegra:**
  - `conf/machine/include/tegra-common.inc`;
  - `recipes-core/initrdscripts/tegra-minimal-init/{init-boot.sh,platform-preboot.sh}`;
  - `recipes-bsp/tegra-binaries/tegra-redundant-boot/nv_update_verifier.service`;
  - `recipes-bsp/tools/setup-nv-boot-control/esp.mount.in` (by-partlabel);
  - `recipes-bsp/uefi/{tegra-espimage.bb,l4t-launcher-extlinux.bb}`;
  - `classes/image_types_tegra.bbclass` (rootfs sizing, `RECFILE` removed,
    `DATAFILE`).
- **RAUC 1.13:**
  - `src/context.c` (booted slot: `rauc.slot=` → `root=` → custom; creates
    the data dir);
  - `src/install.c` (mounted-slot refusal; activation before the
    post-install handler);
  - `src/bootchooser.c` (grub backend);
  - `contrib/grub.conf`;
  - `data/de.pengutronix.rauc.conf` ("allows anyone to control rauc");
  - meta-rauc `rauc-mark-good.service`.
- **GRUB 2.06:** `grub-core/kern/efi/init.c` (disables the UEFI watchdog);
  `normal/menu.c` (falls back to a prompt); `util/grub-editenv.c`
  (truncate + write).
- **Prior art:** meta-mender-community
  `meta-mender-tegra/meta-mender-tegra-jetpack5` (capsule-driven switch,
  `nvbootctrl verify`, gated `nv_update_verifier`). It supports AGX Orin,
  AGX Xavier and Orin Nano, all of which have eMMC for chain B.
  meta-rauc-community `meta-rauc-tegra` covers U-Boot/R32 boards only, so it
  does not apply.
