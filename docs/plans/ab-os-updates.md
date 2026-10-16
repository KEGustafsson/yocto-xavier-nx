# Plan: A/B OS updates with automatic fallback

**Status: v4, being implemented phase by phase (section 6).** Every
decision in section 10 was taken at its recommendation. Phases 0 to 6 are
in the repository, behind `AB_LAYOUT` (off by default), and so is Phase 7's
tooling. Their exit tests, and Phase 7's bench runs, need the bench board.
Section 11 records what each review pass found and how the plan answers it.

Scope: signed, power-fail-safe OS updates for the boat computer (Xavier NX
devkit, SD-slot module P3668-0000, rootfs on NVMe, no SD card), delivered
**over the air**. Two complete root filesystems ("slots") live on the SSD.
- The board fetches signed update bundles from an HTTPS update channel.
- Each update is written to the slot that is not running.
- The board tries the new slot **once**, and goes back to the previous slot
  by itself if the new one does not come up healthy.

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
5. **Over the air.** The board periodically checks an HTTPS update channel and
   downloads a new bundle in the background. Downloads resume after a
   dropped link, a sleep or a reboot, and stay off metered (cellular)
   connections unless allowed. It never installs or reboots on its own
   (D12). Streaming installs with adaptive (changed-blocks-only) downloads
   follow in Phase 6.
6. **Operable from the chart table.**
   - `boat-update status` says what happened and whether an update is
     downloaded and ready.
   - Install with one command, and roll back with one.
   - SSH push and a USB stick remain fallbacks for a boat with no internet.

**Non-goals** (each is picked up again where it matters)

- **In-field firmware (QSPI bootloader) and kernel-DTB updates.** On this
  hardware they cannot be made A/B-safe (section 3). They stay USB-recovery
  operations, as they are today.
- UEFI Secure Boot, fuse burning, dm-verity, a read-only rootfs. The design
  is compatible with them (Phase 8), but signed updates don't need them, and
  burning fuses is irreversible.
- App/container updates. These already move independently via compose files
  on `/data` (docs/05 "Updates").
- **An update server or fleet management (hawkBit, Mender server).** The
  update channel is a few static files on any HTTPS host (section 5.10).
  rauc-hawkbit-updater can be added later without changing the device design.
- Unattended installs and reboots. A reboot while under way is the bigger
  risk, so installing stays an operator action (D12).

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
over-the-air firmware. Phases 2–7, including the OTA delivery (5.10), carry
over; only the boot backend differs.
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
        |        root=/dev/nvme0n1p<N> rauc.slot=<A|B> panic=10 [boat.trial=1]
        | initrd ($disk,gptN)/boot/initrd
        v
   tegra-minimal-initramfs (patched: that slot only, GPT name checked, bounded retry, reboot)
        v
   systemd -> boat-commit.service (health gate) -> OK=1 TRY=0 COMMITTED=1
                                                 or OK=0 + reboot (trial only)
```

**Getting GRUB into the path** (review finding B1):

- Set `L4TDefaultBootMode = 0` in the `L4TConfiguration` overlay that
  `edk2-firmware-tegra` builds: an `fdtput` on the built `.dtbo`, checked
  with `fdtget`. This is one property, so it is rewritten in place rather
  than replacing meta-tegra's overlay (`TEGRA_BOOTCONTROL_OVERLAYS`).
- Add `boat-bootmode-guard`, which runs every boot and rewrites the
  variable with efivarfs if it is not 0. The DT only seeds an absent
  variable, so existing boards and "reset to defaults" in the UEFI menu
  need this.
- **Keep USB media out of the boot path.** The same overlay seeds
  `DefaultBootPriority = "usb,nvme,…"`, which sorts UEFI's boot order on the
  first boot, and `NewDeviceHierarchy = 1`, which puts every USB stick or SD
  card inserted later at the *front* of it. A bootable stick left in (a
  JetPack-flashed one, an installer) would boot instead of the NVMe's
  L4TLauncher and GRUB, so no attempt is counted and nothing commits or
  falls back. The overlay is rewritten to `"nvme,emmc,sd,usb,ufs"` and `0`,
  and `boat-bootmode-guard` sets `NewDeviceHierarchy` to 0 at runtime for a
  board whose variable already exists (only a warning if it cannot: it is
  one of the trial's critical units). Phase 1 confirms on the bench whether
  the locked `DefaultBootPriority` is re-seeded on a board that has it.
- Treat a boot **without `rauc.slot=`** on the cmdline as "booted outside
  GRUB". `boat-update status` reports it loudly, and no install is allowed
  in that state.
- Alternative mechanism if the overlay route misbehaves: a UEFI boot entry
  whose LoadOptions carry `bootmode=grub`, which L4TLauncher also honours.

**Fallbacks below GRUB:**

- If loading `grubaa64.efi` fails, L4TLauncher drops to DIRECT, which boots
  extlinux on `APP` (slot A), then `boot.img`. It does the same when it
  cannot rewrite `boot.cfg` on the ESP. Slot A then runs whatever
  `grubenv` says, so it may be the older or a rejected release. When
  `L4TDefaultBootMode` already read 0, `boat-bootmode-guard` raises an
  alert that GRUB did not load: rebooting comes back the same way, and the
  fix is a reflash.
- `UBOOT_EXTLINUX = "1"` is set because its recipe is also what puts
  `/boot/Image` and `/boot/initrd` into the rootfs for GRUB to read.
  `boat-ab-boot` fails the parse of an A/B build without it.
- `boot.img` is a last resort only. Its kernel is the flash-time one and
  will not match a newer slot's `/lib/modules`.
- **GRUB *starting* and then failing does not fall back.** L4TLauncher only
  falls back when the *load* fails, so `grub.cfg` must never stop at a
  prompt (5.3). Nor may GRUB itself when `grub.cfg` is missing or
  truncated: the config built into `grubaa64.efi` (`meta-boat`'s
  `grub/files/cfg`, in place of poky's) locks GRUB's shell, runs
  `grub.cfg` itself, and resets when it comes back without booting a slot.
- **GRUB hangs: keep the UEFI watchdog armed.**
  - L4TLauncher arms a 5-minute UEFI watchdog before starting GRUB, but
    stock GRUB 2.06 cancels it in its EFI init
    (`grub-core/kern/efi/init.c`: `set_watchdog_timer(0, …)`).
  - A one-line `meta-boat` patch to `grub-efi` **re-arms it instead**
    (300 s).
  - A GRUB that wedges, or a kernel load that never returns, then ends in a
    firmware reset.
  - It covers a hang, not a crash. This UEFI watchdog is edk2's software
    one (`WatchdogTimerDxe`, the only one in NVIDIA's platform
    description): a timer event that resets the board when it expires. A
    synchronous exception inside GRUB - a bad pointer from a damaged
    filesystem, a stack overflow - ends in the firmware's
    `DefaultExceptionHandler`, which dead-loops with interrupts masked, so
    the event never runs. Only the Tegra hardware WDT, if it runs that early
    (Phase 0 runbook 2.3), or a power cycle ends that. A crash while GRUB
    loads the slot comes after `grub.cfg` has saved the slot's `_TRY` count,
    so the next boot falls back (5.5). A crash before that save - in GRUB's
    core or modules, or reading `grub.cfg` and `grubenv` on the ESP both
    slots share - or with a `grubenv` the save could not write, is not
    counted and repeats on every boot whichever slot is chosen: a reflash
    at the dock.
  - The UEFI spec has `ExitBootServices` disarm the watchdog, so it stops
    the moment the kernel takes over. There is no menu (`timeout=0`), so it
    can never fire while someone is reading the screen.
  - Phase 1 proves it with a `grub.cfg` that only sleeps. The Tegra hardware
    WDT, if Phase 0 shows it runs this early, is a second layer, not the
    one the design depends on for a hang - and the only one for a crash.

### 5.2 Disk layout (custom external layout, `boat-nvme-ab.xml`)

This is NVIDIA's `flash_l4t_nvme_rootfs_ab.xml` with these changes:

- `APP_b` has no filename (not written at flash).
- `SWAP` and `DATA` are added at the end, with explicit GPT ids.
- **Pinned GPT entry numbers** for `APP`, `APP_b`, `esp`, `SWAP` and `DATA`
  (`BOAT_AB_PARTNUM_*` in `meta-boat`'s `layer.conf`). The build fails if
  NVIDIA's template ever moves one of them.
- It is **generated at build time** from NVIDIA's file, by a script in the
  `tegra-bootfiles` bbappend, rather than committed. No copy of NVIDIA's
  layout lives in this repository, and a BSP update's changes flow through.

| # (GPT id) | name | size | written at flash? | notes |
|---|---|---|---|---|
| … | kernel, kernel-dtb, kernel_b, kernel-dtb_b, recovery, `esp_alt`, UDA, … | as NVIDIA | as NVIDIA | unchanged |
| 1 | `APP` | `ROOTFS_SIZE_BYTES` (16 GiB) | yes, a **smaller** ext4 image (below) | slot A |
| 2 | `APP_b` | same | **no** | slot B. `B_OK=0` until the first update writes it. |
| 12 | `esp` | as NVIDIA | yes | GRUB, `grub.cfg`, `grubenv` |
| 17 | `SWAP` | 8 GiB | no | `mkswap` on first boot |
| 18 | `DATA` | min 2 GiB, **fill-to-end** (`0x808`) | small ext4 seed image via `DATAFILE` (Phase 2); its journal is written zeros, not a hole, so a flash that skips holes (`bmaptool`) cannot leave the last filesystem's journal under the same UUID | online `resize2fs` on first boot |

Points that need care:

- **Address partitions by NVMe device and GPT entry number, never by
  label** (review M5). A JetPack SD card in the module's slot, or a USB
  stick, can carry `APP`/`esp` labels too. With the module's own SD slot, a
  label lookup could mount someone else's rootfs or write `grubenv` to the
  wrong ESP.
  - Fixed PARTUUIDs were the v4 answer, but they do not survive a flash:
    meta-tegra's `make-sdcard` parses `<unique_guid>` and never passes it to
    `sgdisk`, so every flash gets random ones (found in Phase 1).
  - The devkit has exactly one NVMe drive, always `nvme0n1`; SD cards are
    `mmcblk*` and USB media `sd*`. So `/dev/nvme0n1p<N>` cannot name foreign
    media.
  - Everything that acts on a partition **first checks the GPT name at
    that number** (`APP`, `APP_b`, `esp`, …) and refuses on a mismatch, so a
    re-partitioned or foreign drive in the M.2 slot is not written to.
  - RAUC slots and mount units use `/dev/nvme0n1p<N>`.
  - meta-tegra's `boot-efi.mount` (`by-partlabel/esp`) gets a drop-in.
  - The initramfs patch accepts only `root=/dev/nvme0n1p1` or `p2`, checks
    the GPT name, and removes the label search and the `mmcblk0p1` fallback.
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
  plus 30 % plus `boat-image`'s 512 MiB extra space on this layout), and
  grow it inside the partition later:
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
  about 3× today's ~5 GiB image. With Docker on `/data`, the single-slot
  image's 4 GiB `IMAGE_ROOTFS_EXTRA_SPACE` shrinks to 512 MiB on this
  layout; at 4 GiB it would cap what a slot can hold at about 9 GiB
  ((16 GiB − extra) / 1.3, against `IMAGE_ROOTFS_MAXSIZE`). The limit is
  about 12 GiB of content.
- **`boat-grow-rootfs` must not run on this layout.** It is removed from the
  A/B image and refuses if `APP_b` exists. `boat-storage` only ever grows
  filesystems inside their partitions; it never edits the partition table.
- **Mounts ship in the image** as mount units by partition number (name
  checked), all `nofail`
  apart from the slot's own rootfs. A runtime edit to `/etc/fstab` would be
  lost on update. A broken *shared* resource (ESP, `DATA`, `SWAP`) must
  never drop a slot into emergency mode (5.5).

### 5.3 Slot selection: the GRUB state machine

`grubenv` on the ESP holds:

| variable | owner | meaning |
|---|---|---|
| `ORDER` | RAUC | e.g. `"B A"`; GRUB tries slots in this order |
| `A_OK` / `B_OK` | RAUC + GRUB | slot is usable. RAUC sets 0 before writing a slot; GRUB sets 0 when a slot has used up its attempts. |
| `A_TRY` / `B_TRY` | GRUB counts up; `boat-grubenv-guard` resets a committed slot's to 0 as soon as the ESP is mounted (`boat-commit` again if it could not), and `boat-commit --shutdown` gives a trial its attempt back once | **unconfirmed boot attempts** (a count, not a flag) |
| `A_COMMITTED` / `B_COMMITTED` | ours | 1 once that slot's image passed a trial boot. Cleared for the target by the **pre-install** handler, so a fresh slot is a trial *by default* (review M7). |
| `BOAT_STAY` | ours | bench escape hatch: GRUB adds `boat.stay=1` (no automatic reboots, 5.5). It ends with the first healthy boot that carried it. |
| `BOAT_RETRY` | ours | the slot whose trial already had its one shutdown retry (5.4); cleared by a commit and by the next install. GRUB does not read it. |

GRUB logic (derived from RAUC's `contrib/grub.conf`, with our counter added):

1. `timeout=0`, no menu, GRUB superuser password (no editing at the
   console). The config built into `grubaa64.efi` sets the same lock before
   anything else and runs `grub.cfg`; a missing or truncated `grub.cfg`
   ends in a reset, not a prompt (5.1).
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
   for `initrd`, and for a `boot` that returns. GRUB never stops at a
   prompt. When that mark cannot be saved (`grubenv` unreadable, or the ESP
   refusing writes), a reset would bring the same slot back, each time
   using one of NVIDIA's warm boots. So GRUB hands over within the same
   boot instead, uncounted, to a committed slot (`boat.handover=1`), or to
   the last resort without the slot that failed. `boat-grubenv-guard`
   writes the mark from Linux and raises an alert.
5. If no slot qualifies, the **last resort** boots without counting or
   writing anything, with `boat.lastresort=1`: first a slot that passed a
   trial and only ran out of attempts (`COMMITTED=1`, in `ORDER`), then any
   other, `A` first. A slot that will not load hands over to the next. The
   failed or half-written slot is therefore never preferred to a good
   committed one. `boat-commit` raises an alert on such a boot; it makes a
   committed slot usable again (`OK=1 TRY=0`) and leaves one that never
   passed a trial as it is. If nothing loads, the end state is NVIDIA's
   sticky recovery (below).
6. The last line is `reboot`.

At build time, `grub.cfg` is generated from `KERNEL_ARGS` and checked with
`grub-script-check`. The bundle recipe also fails if the rootfs lacks
`/boot/Image` or `/boot/initrd`. `grub.cfg` passes `boat.esp=N`, the version
of the ESP's boot files (5.9); `boat-grubenv-guard` raises an alert when it
is older than the running release needs, and only notes in the journal that
it is older than the newest (a reflash empties `/data`, so it is never
pressed on a board for an improvement alone).

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
| committed A, one power cut mid-boot | A(TRY 1) → cold boot → A(TRY 2) → `boat-grubenv-guard` resets TRY | stays on A (cold boot also re-seeds NVIDIA's register; Phase 0 confirms this) |
| committed A broken, B good | A → A → B | B runs; reported in `status` |
| both broken | A → A → B → (4th warm boot) | NVIDIA sticky recovery → stops in UEFI. Recovery is a USB reflash, which empties `/data`; a UEFI-menu path that clears the marker without one is still to be found (Phase 0 runbook 2.6), and then documented. |

**`grubenv` integrity** (review M2):

- `grub-editenv` rewrites `grubenv` in place: it truncates, then writes.
  On vfat that is not atomic, and RAUC fails outright if a variable is
  missing. A torn write does not always leave an unreadable file: the ESP's
  FAT32 has 512-byte clusters, so the 1 KiB block spans two, and a cut can
  keep the new first half and a stale second half. GRUB checks only the
  signature at the start, reads the keys of *both* halves (the stale copy
  last, so it wins) and saves into the first: its attempt count never
  advances. So **no writer edits `grubenv` in place**:
  - `boat-commit`, the guard, the RAUC handlers and `boat-update` write a
    copy, edit it, rename it over `grubenv`, flush, and copy it to
    `grubenv.bak`, under one lock (`/run/boat-ab/commit.lock`, mode 0600
    from a tmpfiles.d entry: `flock` works on a file opened read-only, so a
    lock any user could open would let them hold every writer off). A writer
    that cannot have the lock within a minute writes nothing - one that went
    on without it could undo another's decision - and GRUB's attempt count
    decides instead;
  - RAUC's grub backend runs `grub-editenv` by name, and `rauc.service` has
    `/usr/lib/boat-ab/bin` first on its `PATH`: the `grub-editenv` there
    turns a `set` on `grubenv` into the same locked copy-and-rename, and
    passes everything else to the real one. It fails RAUC's call only when
    `grubenv` itself was not written; a failed `grubenv.bak` copy after that
    is an alert, and the guard copies it on the next boot.
- `boat-grubenv-guard.service` runs early on every boot, after the ESP
  mount and before `boat-commit`. It validates the file and keeps a
  last-good copy, `grubenv.bak`. On a committed boot it also resets the
  slot's attempt count at once (not on a trial or a last-resort boot). A
  failure is an alert, and does not stop `boat-commit` (`Wants=`).
- If the file is invalid, the guard rebuilds it from what this boot proves,
  and says so (`/run/boat-ab/grubenv-repaired`, `status`):
  - on a trial boot, the booted slot stays first and on trial
    (`TRY=1, COMMITTED=0`), and the other is its committed fallback - no
    install starts during a trial, so it is the slot the update came from;
  - on a committed boot, the booted slot becomes `OK=1, COMMITTED=1`, and
    the other `OK=0, COMMITTED=0`: the torn write may have been RAUC's own,
    half way through writing it, and `grubenv.bak` predates this boot. A
    rollback is refused until the next update rewrites that slot;
  - on a last-resort boot (`boat.lastresort=1`), GRUB passes the same flags
    whether or not the slot ever passed a trial, so the booted slot becomes
    `OK=1, TRY=0, COMMITTED=0`: on trial from the next boot, with the health
    gate, rather than committed unchecked. The other slot is `OK=0,
    COMMITTED=0`, as on a committed boot.
- `grub.cfg` falls back to `grubenv.bak` for *reading* if `grubenv` won't
  load.

### 5.4 What "booted correctly" means

There are two tiers, so that a broken *data* partition or an app problem can
never flip the OS back to an older slot.

- **Tier 1 (every boot):**
  - the kernel booted the slot named by `rauc.slot`, from the partition
    that slot is configured for, and its GPT name matches;
  - the rootfs is read-write;
  - the ESP is mounted;
  - `grubenv` is valid;
  - `rauc status` works - checked only when `/data` is the `DATA`
    partition, since RAUC keeps its status there: on a board whose DATA is
    broken a trial commits without this check (a broken `/data` never fails
    the OS, 5.6).
- **Tier 2 (trial boots only, `boat.trial=1`):**
  - critical units are active: `NetworkManager`, `sshd`/`sshd.socket`,
    `systemd-logind`, `dbus`, `boat-bootmode-guard`, and `docker` if `/data`
    is mounted;
  - units the operator lists as needing `/data` (`BOAT_HEALTH_DATA_UNITS`,
    e.g. `boat-compose`), only when `/data` is mounted;
  - no failed units in a configurable critical list;
  - optional operator checks in `/data/boat-update/health.d/*`, **off by
    default**: run as root with no display, each with a timeout (`-k`: a
    check that ignores SIGTERM is killed) that never reaches past
    `boat-commit.service`'s own limit, their output in the gate's journal.
  - A `*.socket` stopped by its trigger limit (a connection flood) is
    started again, not held against the release; `sshd.socket` has no
    trigger limit to trip (a drop-in), so a flood is refused per
    connection.
- **Never part of either tier:** anything external (internet, NTP, GNSS,
  registry pulls, NMEA).

Mechanics (review M3: meta-rauc's `rauc-mark-good.service` is
`WantedBy=multi-user.target`, which orders `multi-user.target` *after* it, so
a "multi-user reached" check would deadlock):

- Leave meta-rauc's `rauc-mark-good` package out of the image (the `rauc`
  recipe only recommends it). One unit does the gating and the marking:
  `boat-commit.service`. It is `Type=oneshot` and
  `DefaultDependencies=no` (so no target orders after it), `After=` the
  critical units, `WantedBy=multi-user.target`, and retries checks for up to
  `TimeoutStartSec=10min`. It is *not* ordered after Docker or `data.mount`,
  which wait for `/data`'s check (no time limit). Instead the script does
  not decide a trial while DATA's check or mount is still running or
  queued - its `/data` checks would be skipped, not passed - and waits,
  within `BOAT_HEALTH_DEADLINE`; a DATA still pending then is a data
  problem, and the trial is decided without it. So nothing on `/data` can
  hold up, or fail, the verdict. It conflicts with `emergency`/`rescue`
  (5.5).
- `/proc/uptime` counts time asleep and the deadline timer does not: a
  sleep during the retries (which 5.6 refuses) gives the checks that time
  back rather than ending them.
- On success: `<slot>_OK=1 TRY=0 COMMITTED=1` in **one atomic `grubenv`
  write** (copy, `grub-editenv`, rename), read back. Not `rauc status
  mark-good`: RAUC's grub backend rewrites `grubenv` in place, and a power
  cut that tears that write sends GRUB to `grubenv.bak`, where the passed
  trial still looks used up.
- **Trial failure:** `<slot>_OK=0` the same way, record the reason in
  `/data/boat-update/last-result` and append it to `history` (best effort,
  and only if `/data` is the `DATA` partition), then reboot. GRUB skips the
  slot. Every verdict - commit, failure, shutdown retry - goes into
  `history` this way.
- **A board fault fails the trial too.** A Tier 1 failure (the ESP, `grubenv`,
  RAUC) or a commit the lock or the ESP refused can be the board's fault,
  not the release's, but `boat-commit` cannot tell which - a release can
  break each of them - so the trial still fails and the release is still
  not offered over the air again. Its record says so, and that
  `boat-update install <bundle file>` installs it again once the board is
  fixed.
- **Committed boot:** the attempt counter is reset *first*, before any
  check that may take a while - by `boat-grubenv-guard`, as soon as the ESP
  is mounted, and by `boat-commit` if it is still set. A reset the ESP
  refuses is an alert: while it lasts every reboot counts.
- **Committed Tier 1 failure:** no mark, no reboot. An alert shows in
  `status`. The attempt counter is still reset, since the slot did boot, so
  the slot keeps running and the GRUB counter only hands over if it then
  *also* fails to boot, twice.
- **Last-resort boot** (`boat.lastresort=1`): always an alert. A committed
  slot is made usable again; a slot that never passed a trial is not, and
  is never committed here.
- **Debug hold:** `BOAT_STAY=1` is for the boot being debugged. A healthy
  boot that carried `boat.stay=1` - a passed trial, or a healthy committed
  boot - sets it back to 0, so a forgotten hold cannot keep every later
  failure from resetting.
- `boat-trial-deadline.timer` (`ConditionKernelCommandLine=boat.trial=1`,
  `OnBootSec=15min`): if the trial still isn't committed, mark-bad and
  reboot. This catches a commit unit that hangs. The gate and the deadline
  take one lock, so only one of them decides, and a commit stops the timer.
  The deadline goes by this boot's own record of a commit that fully
  succeeded (`/run/boat-ab/committed`), not by `COMMITTED=1` in `grubenv`:
  a write that failed after its rename can show `COMMITTED=1` that was never
  flushed, or that `grubenv.bak` does not have.
- `--deadline`, `--shutdown` and `--may-sleep` never read `health.conf`, so
  an operator's mistake there cannot also take away the deadline, the retry
  or the sleep hold. The gate reads it in a subshell first: one it cannot
  read is an alert, and the defaults decide. If the deadline's own run fails
  anyway, its unit reboots (`ExecStopPost`), and GRUB's count ends the trial.
- **The sleep inhibitor** is held from install until the trial is committed,
  not just during the write. Shutdown is not held off: the skipper must be
  able to turn the computer off. Instead, a clean shutdown or reboot before
  the verdict gives the trial its attempt back, once (`boat-trial-inhibit`'s
  `ExecStop`, `boat-commit --shutdown`), so the release is tried again
  rather than failed and never offered again. Not after the verdict, not for
  a forced reboot (`systemctl --force` skips it), and a second one in the
  same trial counts (`BOAT_RETRY`). Nor when GRUB would boot the other slot
  next - `boat-update rollback` off an undecided trial: that is a verdict,
  so the slot is marked bad, the release recorded as failed, and it is not
  offered again.
- **Not checked, deliberately:** the helm display (Xorg, the XFCE session)
  and the operator's compose stack. A board must pass a trial headless, and
  apps are not the OS; `boat-update rollback` is the answer to a release
  that breaks them, and `BOAT_HEALTH_DATA_UNITS`/`health.d` are there for
  an operator who wants them gated. Nothing at the helm shows a trial or an
  ALERT either: `boat-update status` is where the board reports (goal 6).
- `nv_update_verifier.service` keeps running unconditionally. It only feeds
  NVIDIA's counter. Phase 1 confirms that it also resets that counter while
  booted from `APP_b` (≥4 warm reboots on B).

### 5.5 Making every failure end in a reset

A failover only happens on the *next* boot, so every way of failing must end
in a reset. The exception is a deliberate stay on a committed slot.

| failure | what turns it into a reset | change needed |
|---|---|---|
| firmware fails before UEFI | not covered (single chain); firmware never changes in the field | none |
| `grubaa64.efi` missing/unloadable | L4TLauncher falls back to DIRECT → extlinux on `APP` (A) → `boot.img` | none; `status` flags "booted outside GRUB", and `boat-bootmode-guard` alerts that GRUB did not load when `L4TDefaultBootMode` already read 0 (a reboot will not help: reflash). Slot A runs whatever `grubenv` says, and with no `panic=10`: a reset loop there would only reach NVIDIA's sticky state sooner, and a slot-aware DIRECT path needs its own design |
| `grub.cfg` missing or truncated | the config built into `grubaa64.efi` runs it, and resets when it returns (5.1) | `grub-efi` bbappend: boat's `cfg` |
| kernel / initrd load error, `boot` returns | guarded loads → mark slot, `reboot`; when the mark cannot be saved, a hand-over within the boot (5.3) | grub.cfg design + build checks |
| GRUB itself hangs (a loop, a stall, a disk read that never returns) | **the UEFI watchdog, kept armed through GRUB** (300 s; 5.1). The Tegra hardware WDT is a second layer if Phase 0 shows it runs this early. | `grub-efi` bbappend: re-arm instead of cancel. **Phase 1 exit test.** If firmware ignores the re-arm, this row narrows to "power cycle to recover" and goal 4 says so. |
| GRUB itself crashes (a synchronous exception: the firmware's handler dead-loops with interrupts masked) | **not the UEFI watchdog**, a timer event that never runs then (5.1). Only the Tegra hardware WDT, if Phase 0 (runbook 2.3) shows it runs this early; otherwise a power cycle. A crash loading the slot comes after its attempt was counted, so that boot falls back; one before the count was saved (GRUB itself, the shared ESP, an unwritable `grubenv`) repeats whichever slot: a reflash | none possible in GRUB; Phase 1 records what a GRUB exception does on this UEFI |
| kernel panic | `panic=10` | cmdline |
| kernel hard hang | Tegra WDT running from early kernel | Phase 0: armed before userspace? If not, enable it at probe |
| initramfs can't mount root | today: **hangs forever** | `tegra-minimal-init` bbappend: only the two slot partitions, GPT name checked, bounded retries, then `reboot -f` (`boat.stay=1` keeps the shell) |
| systemd emergency/rescue | today: waits forever | drop-ins: reboot after 60 s unless `boat.stay=1`. Shared mounts are `nofail`, so only slot-local faults get here. `boat-commit.service` conflicts with both modes: it has no default dependencies, so it would otherwise still run there and reset a committed slot's attempt count, and GRUB could never fall back from a slot that keeps ending in emergency mode. A `systemctl rescue` typed on purpose reboots too (docs/07 says how to keep it). |
| PID 1 wedged | `RuntimeWatchdogSec=60s`, `RebootWatchdogSec=10min` | **decision D8**: one `/dev/watchdog` opener |
| commit/health hangs on a trial | `boat-trial-deadline.timer` | add |
| power cut during a trial | TRY already counted → next boot skips the trial slot | none (conservative) |
| clean shutdown or reboot during a trial | attempt given back once (5.4) → the trial runs again | `boat-commit --shutdown` |

The `boat.stay=1` escape hatch comes from the **cmdline** (GRUB, from
`BOAT_STAY`), not from a file on `/data`. `/data` may not be mounted in
emergency mode.

### 5.6 The install sequence, and what a power cut does at each step

`boat-update install` is a thin wrapper around `rauc install`. It takes the
bundle that is downloaded and ready (5.10), or an explicit path from an SSH
push or USB stick, or with `--stream`, the channel URL (Phase 6). Whatever
the source, the steps below are the same.

- It refuses unless booted via GRUB (`rauc.slot` present) and `/data` is a
  real mountpoint. Otherwise RAUC would `mkdir` its data directory on the
  rootfs.
- It holds a sleep and shutdown inhibitor while it installs; after the
  reboot, `boat-trial-inhibit` holds off sleep until the commit (5.4).
- No suspend at all while an update is being installed or is on trial
  (`boat.trial=1` and no commit recorded this boot): those inhibitors are
  root's, and logind never holds root back with root's own locks, so
  `boat-ab-boot` puts `boat-commit --may-sleep` in front of
  `systemd-suspend` (and hibernate's kin), which fails the suspend job
  whoever asked - root's own `systemctl suspend` too. `boat-sleep` refuses
  as well, `--force` or not, and its `--status` and `--dry-run` say why.

The order matches RAUC 1.13's `install.c`:

| step | who | if power is lost here |
|---|---|---|
| 1. verify signature and verity format | RAUC | nothing changed |
| 2. system `pre-install` handler: BSP/L4T, device-tree (`firmware=`) and Docker-version checks from the bundle's `[meta.boat]` manifest section; room on `/data` for RAUC's status file (RAUC marks the target `OK=0` before its first save there); step 3's checks done first (`compatible` against `system.conf`, `min-bundle-version` by RAUC's own version rules), so a bundle RAUC will refuse is refused here before anything changes; board settings the target holds and this slot does not are kept on `/data` (5.8); then set target `COMMITTED=0` (and nothing else: the target must stay a usable fallback if anything still fails) | ours | target merely marked not-committed |
| 3. check `compatible` and `min-bundle-version` (5.7) | RAUC | as step 2: a bundle that fails here never got past step 2 |
| 4. set target `OK=0` (through boat's `grub-editenv`: a locked copy and rename, 5.3) | RAUC grub backend | target unbootable; stay on current |
| 5. write image into the target slot, `resize=true` | RAUC (`type=ext4`) | target half-written, `OK=0` |
| 6. slot `post-install` hook: carry-over into the new slot (5.8) | ours | target still `OK=0` |
| 7. activate: `ORDER=target first`, `OK=1`, `TRY=0` | RAUC | new slot boots **as a trial** (COMMITTED=0 since step 2). If `grubenv` is torn, see 5.3 integrity. |
| 8. system `post-install` handler: append history on `/data` | ours, best effort | none |
| 9. reboot (operator or `--reboot`) → trial → commit or fallback | 5.3 / 5.4 | TRY counted → fallback |

- **No post-write read-back hash.** RAUC resizes the filesystem before slot
  hooks run, so a hash taken in the hook would never match.
- **Manual rollback:** `boat-update rollback` writes what RAUC's
  `mark-active other` would - `ORDER` with the other slot first, its `OK=1`
  and `TRY=0` - itself, under the A/B lock, and reboots. Not through RAUC:
  `rauc.service` needs `/data`, and a release that broke `/data` still
  commits (the health gate never fails the OS over data). It refuses if
  the other slot is `OK=0`. It is a slot switch; nothing is reinstalled,
  so it warns that credentials changed since the other slot's install are
  back as they were there (5.8).
- **Transport:** OTA from the update channel (5.10) is the normal path.
  Fallbacks:
  - an SSH push to `/data` (`scripts/push-update.sh`);
  - a USB stick. A bundle of several GB exceeds FAT32's 4 GiB file limit,
    so the stick must be exFAT or ext4.
- **A stream that fails mid-install is safe.** RAUC aborts, and the target
  is still `OK=0` from step 4, so the board stays on the current slot. A
  retry starts the install again.

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
  from a container. The policy goes by uid, so a container running as root
  (Docker's default, with no user-namespace remapping) passes it: a
  container that is given the system bus must run as an unprivileged user.
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
  - `lint.sh` fails if any tracked file contains a PEM private-key header (`-----BEGIN ... PRIVATE KEY-----`).
- **Fault-injection bundles** (section 7) are signed by a **separate bench
  CA**, trusted only by images built with `BOAT_BENCH=1`. A boat image
  never trusts it.
- **Production:** the CA private key stays offline, and the signing key can
  live on a PKCS#11 token (`RAUC_KEY_FILE` as a `pkcs11:` URI and
  `RAUC_PKCS11_MODULE` in `local.conf`; `boat-bundle` then builds
  `libp11-native`). A token that needs a PIN to sign is not supported:
  bitbake would record it in its signature files.
- **Compatibility and downgrade:** `compatible=boat-xavier-nx` rejects
  bundles for other boards. RAUC's `min-bundle-version` (D6) covers
  downgrades on every install path: `BOAT_MIN_BUNDLE_VERSION` in
  `scripts/env.sh` (or the kas environment), raised in the release after a
  security fix; it reaches the build through `02-configure-build.sh`
  (`local.conf`), and `03-build.sh` refuses a floor or release there, or a
  key directory exported in its shell, that `local.conf` does not have. The build refuses a
  floor above its own `BOAT_RELEASE`. Falling back after a failed update is a slot switch, not an
  install, so the version policy never blocks a failover.
- **Attack surface:** no new listening port. OTA is **pull-only**: the
  board makes outbound HTTPS requests to the channel and nothing connects
  in. Manual installs still go over the existing SSH or from local media.
- **The OTA channel is not trusted for authenticity** (5.10). RAUC verifies
  every bundle against the keyring whatever its source, so a compromised
  server or a TLS interception can at most:
  - **withhold updates** (a freeze attack). `status` shows the age of the
    last successful check and the channel's publish date, so a stale
    channel is visible. It cannot be prevented without an online
    time-stamped authority; accepted. A withdrawal can be withheld too: a
    board that has not seen the withdrawing index can be kept on the one
    before it, and fetch the withdrawn - still validly signed - release
    (5.10).
  - **waste bandwidth or offer an old bundle.** This is blocked *before*
    downloading. The index is signed by its own key (5.10), and the board
    checks its `channel`, a monotonic `serial` (no replay), the version
    against both slots and the failed list, `size` and SHA-256. Downloads
    are size-capped, redirect-free and limited in re-fetches. A streamed
    install cannot be checked first (RAUC reads the bundle as it writes
    it, and follows redirects), so it is checked straight after: the
    slot must hold the release the index named, or it is marked bad
    before anything boots it (Phase 6).
  - **learn the board's version** from its requests. TLS keeps this from
    passive observers; a client certificate (D15) keeps it from the public.
- **Index verification is pinned to our CA only.** OpenSSL's default store
  (`/etc/ssl/certs`) is explicitly excluded, so a public certificate can
  never sign an index (5.10).
- **Credentials:**
  - The only supported credential is a **TLS client certificate**, in
    `/data/boat-update/` (key mode 600, root only: only `boat-update`
    uses it, also for streaming - RAUC streams through its relay, Phase 6).
    It grants **read** access to the channel only. Publishing uses a
    separate credential that stays on the build host.
  - **Basic auth and bearer headers are not used:** they would follow
    redirects to other hosts (and to `http`) and be written to the journal
    by RAUC's streaming helper.
  - Nothing credential-bearing is ever put on a command line.
- **Honest limits:**
  - This authenticates *updates*. Root on the running board can still write
    anything to disk, and the bench image's shared password (README
    "Credentials") still applies.
  - A bundle is signed, not encrypted (a `verity` bundle): anyone who can
    fetch one reads the image, `/etc/shadow` included. With the
    repository's `BOAT_PASSWORD_HASH` that discloses nothing new; a build
    with a private hash needs a channel with a client certificate (5.10).
  - A physical attacker with USB recovery mode can reflash.
  - Secure Boot and dm-verity (Phase 8) close those gaps. For option A that
    also means signing GRUB and having it verify the kernel.

### 5.8 State that must survive a slot switch

`/etc` and `/var` belong to the slot, and an update replaces them.

- **Carry-over at install** (a `slot-post-install` hook inside the bundle
  copies these from the running slot into the new one, so the new
  release's list is the one that runs; early-boot safe, and each slot stays
  self-contained for rollback):
  - `/etc/machine-id`;
  - `/etc/ssh/ssh_host_*`;
  - `root`'s `~/.ssh/`, `/home/root/.ssh` on this distro (authorized keys:
    docs/05 recommends key-only SSH, so losing them locks you out, review
    M8);
  - `/etc/boat-sleep.key`;
  - `/etc/NetworkManager/system-connections/`;
  - `/var/lib/bluetooth/`;
  - `/etc/hostname` and `/etc/localtime`;
  - the operator-edited `CONFFILES` (`/etc/default/boat-power`,
    `/etc/NetworkManager/conf.d/90-boat-wol.conf`,
    `/etc/default/boat-bluetooth`, `/etc/bluetooth/main.conf`,
    `/etc/default/boat-sleep-listener`, `/etc/docker/daemon.json`,
    `/etc/boat-update/health.conf`) - but only if someone changed them.
    The image records the SHA-256 of every file it ships in `/etc`
    (`/usr/lib/boat-ab/conffile-defaults.sha256`); a file that still
    matches is left as the new image has it, so a release can change its
    defaults (a renamed unit in the health gate's list, a `daemon.json` a
    new Docker needs). A file shipped in the image and later deleted
    stays deleted. An edited file is carried as it is, never merged; the
    new release's own version goes beside it as `<file>.boat-default`. The
    one exception is `health.conf`'s unit list: when a carried list names a
    unit the new slot does not have, the hook appends a merged list (the
    board's units the new slot has, then the release's), or every trial
    would fail on it. An image from before the record carries them all,
    and the carried files then look edited at every later update too,
    until someone takes the `.boat-default` or reflashes. `daemon.json` is
    not checked: an edited one a new Docker rejects fails the trial;
  - the password hash fields of `root` and `boat`, merged (**D5**);
  - `/etc/nv_boot_control.conf`.
  - Not on this list: the OTA channel configuration. Its defaults ship in
    the image (`/etc/boat-update/channel.conf`), and the operator's
    overrides and client certificate live on `/data` (5.10), so both slots
    always see the current values.
- **Lives on `/data`:**
  - Docker and compose (already there);
  - RAUC's `data-directory` (`/data/rauc`; `rauc.service` is
    `Requisite=`/`After=data.mount`, so it waits for a mount under way but
    does not restart a failed DATA check on every on-demand start);
  - `boat-update` history;
  - a size-capped persistent journal (`/data/log/journal`), so
    `journalctl -b -1` after a fallback shows why the trial failed
    (`SystemKeepFree=512M`, well below what `boat-update` keeps free, so a
    nearly full DATA does not stop it growing);
  - **the login users' homes** (D11): `/home/boat`, bind-mounted from
    `/data/home/boat` (`BOAT_AB_HOME_USERS`). This keeps `~boat/.ssh`, the
    XFCE settings and anything users keep in their home across updates. It
    is seeded from the image's copy on first boot. `root`'s home is
    `/home/root` on this distro and stays in the slot, so key-only SSH as
    `root` still works when `DATA` is what broke; its `.ssh` is carried
    over instead (above), and nothing else in it is: a `docker login` there
    (`boat-compose` pulls as root), a `.gitconfig`, scripts are missing
    after the next install, and a rollback brings back that slot's older
    copy (docs/07 says so). The home mounts come before
    `systemd-user-sessions`, so the helm desktop and non-root logins wait for
    DATA's check, however long a repair takes - deliberately, rather than
    log anyone into the slot's stale copy; the check says so on the console.
  - Firefox, when `boat-install-firefox` installs it (`/data/opt/firefox`,
    with a boot unit that puts its launcher and menu entry back into a new
    slot): in a slot it would be lost at every update, and a rollback would
    start an older Firefox on a profile a newer one wrote.
- Documented consequence: local edits to any other `/etc` file do not
  survive an update. Put them in `meta-boat` or on `/data`.
- The carry-over runs at an install, from the running slot. After a
  rollback or a fallback, the slot that runs lacks what was changed in the
  other one since its install (a Wi-Fi network, a pairing, a key). The
  next install would overwrite those: the `pre-install` handler first
  mounts the target read-only (`noload,nosuid,nodev,noexec`) and keeps
  every connection, pairing and `authorized_keys` that differs from the
  running slot's, and changed after the later of the two slots' installs,
  in `/data/boat-update/displaced/slot-<X>-<time>/`, with an alert. The
  install times are RAUC's `installed.timestamp` in its central status
  file (`rauc status` cannot be asked during the install; a marker written
  by the bundle hook would be missing from slots older bundles installed).
  On a forward update the target is the older slot, whose files predate
  this slot's install: stale copies, not kept. With neither slot recorded
  (a reflash empties `/data` and does not write `APP_b`) nothing is kept.
  Limit: the comparison trusts the clock at the install and at the change.
  Copying them back is left to the operator; nothing merges them on its own.
- The same holds for credentials, with the opposite effect: after a
  rollback or a fallback, a password changed, a key removed from `root`'s
  `authorized_keys`, a sleep key or a Wi-Fi password rotated on the other
  slot since this one was installed is valid again. Nothing can undo that
  for a fallback, which no code on the board chooses; `boat-update
  rollback` warns before it reboots, and docs/07 tells the operator to
  revoke or change them again. Keeping them on `/data` instead would lose
  `root`'s key-only SSH when DATA is what broke (above), and copying them
  into the other slot at a rollback would mean writing a slot from the
  running one, outside RAUC.
- `/data` is shared, so a newer Docker could migrate `/data/docker` in a way
  an older slot can't read. The `pre-install` handler warns when the Docker
  major version differs: an alert in `status` until the reboot, and in the
  install's line in `boat-update history` (RAUC shows a handler's output to
  nobody on a successful install). It does not refuse, and `rollback`
  does not check it. The same holds for other formats on `/data` a newer
  slot may write: a systemd from 252 on writes compact journal files that
  v250's `journalctl` cannot read.

### 5.9 What stays outside A/B, deliberately

| what | why | how it changes |
|---|---|---|
| QSPI firmware (one usable chain) | section 3 | USB recovery (`05-flash-nvme.sh`) |
| kernel DTB (`kernel-dtb`, read by UEFI) | option A trade-off | USB reflash. Phase 0 confirms GRUB passes UEFI's DTB through unchanged. |
| ESP contents: L4TLauncher, GRUB, `grub.cfg` (with `KERNEL_ARGS`) | single copy | USB reflash (`grubenv` and `boot.cfg` are runtime-written). An update never changes them. `grub.cfg` passes their version as `boat.esp=N`; every change bumps it (with `BOAT_ESP` in `boat-grubenv-guard`); a release whose scripts come to need the change also raises `BOAT_ESP_MIN`, and the guard alerts on a board whose ESP is older than that. Nothing refuses such a bundle before it is installed yet. |
| `kernel`/`kernel_b` (`boot.img`) | last-resort fallback only | flash time |

A BSP bump (new L4T) is therefore always a **full USB reflash**. The
`pre-install` handler rejects a bundle whose `[meta.boat] l4t=` differs from
the running `/etc/nv_tegra_release`.

The same L4T release can still bring another kernel DTB: a meta-tegra or
kernel change, or a device-tree overlay added in meta-boat. So each A/B
image records the DTB and plugin-manager overlays it was built against -
the deployed files the flash writes - as one SHA-256
(`/usr/lib/boat-ab/firmware.id`), the bundle carries it as `[meta.boat]
firmware=`, and the `pre-install` handler refuses a bundle whose value is
not the running slot's (which was flashed with it, or checked the same
way). `publish-update.sh` refuses a bundle whose value is not the channel's
current release's unless it is published with `--reflash`. The index
carries the value (`firmware`), and `boat-update` does not offer a release
whose value is not the board's own: it says "reflash at the dock", as for a
BSP bump and not as an alert (5.10), so no board downloads a bundle its
handler would refuse. Limits: a board flashed before the record cannot
check (a warning, and the release is offered); UEFI and the rest of
QSPI are not in it, since nothing shows their builds are reproducible; and
a product build should pin meta-tegra to a commit, as `01-fetch-layers.sh`
says.

**Every USB flash also rewrites `DATA`.** The flash writes the empty
`boat-data` seed into it (5.2), so `/data` - Docker, the homes kept there,
the saved journal, RAUC's and `boat-update`'s state - starts empty again,
as it does on the very first flash. Copy off what you need first. A flash
that leaves `DATA` alone (an empty `DATAFILE`, so `initrd-flash` drops that
line and writes nothing there) is later work (Phase 8).

### 5.10 Over-the-air delivery

OTA is about how a bundle *reaches* the board. Everything in 5.3–5.6 still
applies once it is there. Delivery comes in two stages:

- **Stage 1, download-then-install (Phase 5).** The default, and the robust
  path on a link that keeps dropping.
- **Stage 2, streaming with adaptive updates (Phase 6).** The bandwidth-lean
  path on a link that stays up.

#### The update channel

The channel is static files on **any HTTPS host that supports HTTP Range
requests**: nginx, Caddy, or S3/R2/B2-style object storage. Range support is
what makes both resumable downloads and streaming work. GitHub Releases is a
poor fit, because bundles carrying CUDA are likely to exceed its per-file
size limit (D13).

```text
https://<host>/boat-xavier-nx/<channel>/
    index.p7                                  signed index (one file; see below)
    boat-bundle-<version>-<sha256[:12]>.raucb bundle, content-addressed name
```

**The index is one attached-signature CMS file.** The JSON inside is
readable only after verification. One file means a publish can never
expose a new index next to an old signature. Fields:

| field | purpose |
|---|---|
| `channel` | must equal the channel the board follows, so a `testing` index served at the `stable` URL is rejected |
| `serial` | increases with every publish (at least the time in seconds, below 2^32). The board stores the highest serial it has accepted on each channel (on `/data`) and rejects lower ones, which stops a replayed or pulled old index - on a board that has seen the newer one (a board that has not can still be served the older index: the freeze of 5.7). |
| `compatible`, `version`, `l4t`, `firmware` | checked before any download (below); `firmware` (the bundle's device-tree record, 5.9) only when the index and the board both have one |
| `bundle` | a bare file name: no `/`, no `..`, no scheme |
| `size`, `sha256` | transport integrity of the download |
| `published` | UTC. Shown in `status`, so a frozen channel is visible. |
| `requires_reflash` | this release is a BSP bump, which cannot ship as a bundle (5.9). Informational: a board on another L4T is told to reflash for any *newer* release on another L4T, flagged or not; a board already on the bundle's L4T takes it over the air. |
| `notes` | display only. Size-capped, control characters stripped. |

**Signing uses its own key** (review M1 on v3). An *index-signing* leaf
certificate sits under the same CA as the bundle signer:

- Its EKU is **emailProtection only**. RAUC rejects it for bundles
  (RAUC's own `codesign-rauc` purpose), and the bundle key never has to sit
  on the publish host, so it can stay offline or on PKCS#11.
- The CA certificate carries **no EKU**.
- Publish (`scripts/publish-update.sh <channel> <bundle>`, on the build
  host):

  ```text
  openssl cms -sign -nodetach -binary -outform DER \
      -signer index-signing.cert.pem -inkey index-signing.key.pem \
      -in index.json -out index.p7
  ```

  It uploads the **bundle first and `index.p7` last**, so the index never
  points at a file that isn't there yet. `serial` is the largest of a
  counter kept next to the keys (plus one), the channel's current index's
  serial (plus one; read back from the destination, best effort) and the
  time in seconds, under a lock next to the counter, and is recorded
  before anything is uploaded; an unreadable counter stops the publish.
  The published files are 0644 and directories 0755 whatever the
  publisher's umask. Publishing needs OpenSSL 3 (`-no-CAstore`); rsync
  needs no particular version on either end (no `--mkpath`).
- **Verify on the board.** Every flag below is load-bearing:

  ```text
  openssl cms -verify -inform DER -binary -in index.p7 -out index.json \
      -CAfile /etc/rauc/ca.cert.pem -no-CApath -no-CAstore \
      -purpose smimesign -no_check_time
  ```

  - `-no-CApath -no-CAstore`: without them OpenSSL also trusts
    `/etc/ssl/certs`, so any public certificate could sign an index.
  - `-binary`: matches how the index was signed (no CRLF canonicalisation).
  - `-purpose smimesign`: OpenSSL 3.0 has no codesign purpose, and this
    matches the emailProtection EKU.
  - `-no_check_time`: the board's clock may be wrong, and RAUC likewise
    ignores certificate time (5.7).

  Only the `-out` file is ever parsed. The `openssl` CLI is already on the
  image: `ca-certificates` pulls in `openssl-bin`.
- **Considered instead:** `rauc info --output-format=json <bundle URL>`
  streams only the CMS from the bundle's tail and uses RAUC's own trust
  rules. It needs NBD and root, and it does not check `compatible` itself.
  Kept as a Phase 6 extra check, not the stage-1 gate.

**Channels:** `stable` (the boats); `testing`, followed by a test board
that runs an ordinary boat image, where each release goes first; and
`bench`, for the validation bench's `BOAT_BENCH` images and bundles
(section 7, [the validation runbook](ab-validation.md)). A bench image has
its own `compatible` and trusts the bench CA, so it takes nothing built for
a boat: the bench validates the design, `testing` the release. To promote a
release that committed on the `testing` board, re-publish that same bundle
file with a new `stable` index. Bundles are never renamed, because names
are content-addressed.

**Withdrawing a release** is publishing the one before it again: a new
serial offering an older version. A board drops the offer and its download
(ready or not) at its next check that reaches the channel; one that does
not reach it keeps a ready download, and `install` installs it. A trial is
decided by its health gate alone; a board that committed the release stays
on it ("up to date") until `boat-update rollback` or a fix-forward with a
higher `BOAT_RELEASE`. Deleting `index.p7` or the bundle withdraws nothing
(only a signed index does): boards keep what they have and alert on the
failed check, and `install` warns about it. docs/07 says so.

**Forward compatibility.** A board takes the channel's one current release
straight from whatever it runs, and the running - possibly much older -
release's code judges it. So every bundle and index must stay readable by
the oldest release in the field:

- the bundle: RAUC 1.13's verity format and manifest, signed by a
  codeSigning certificate under the CA in its keyring (5.7);
- the system `pre-install` handler, which is the running slot's: the
  `[meta.boat]` keys `l4t` and `release` (`release` must be the version
  `boat-update` installs: the index's for a stream, the manifest's for a
  file), `firmware` where both sides have it;
- `boat-update`'s index: the required fields and their types, the version
  grammar (`boat-update`'s `parse_version`, the handler's `semver_le`,
  RAUC's own for `min-bundle-version`), the emailProtection index
  certificate and the CMS flags above. Unknown index fields are ignored,
  so a new field means nothing to a release that does not know it.

The bundle's `slot-post-install` hook runs from the new bundle, so the
carry-over may change from release to release. A change to anything in the
list needs a stepping stone - a release that reads both, published until
every board has committed it - or a reflash of the boards left behind.
Nothing enforces this: the index has no "minimum source release" field
(older clients would ignore one anyway), and nothing records which release
last wrote `/data`. A release that must convert data on `/data` does it at
boot in the new slot, idempotently, and in a form the older slot still
reads after a fallback (the Docker and journal cases in 5.8).

**Access:** the default is a **public** channel. A bundle carries the
image's `/etc/shadow`, which is nothing secret while `BOAT_PASSWORD_HASH`
is the repository's (README "Credentials"). A build with a private hash
publishes that hash with every bundle, so its channel needs a **TLS client
certificate** (D15), as does any channel that wants access control.
Basic auth and bearer headers leak in this setup, because curl and RAUC
forward them across redirects and RAUC logs request headers (review M3).

#### On the board: check, download, ready (stage 1)

`boat-update` gains three verbs, and `status` reports all of their state.
**`check`, `download` and `install` share one `flock`.**

- **`check`:**
  1. **Gates.** It runs only when:
     - chrony reports a synchronised clock (TLS fails at 1970 anyway);
     - the booted slot is not on trial (no OTA during a trial): booted as
       one and not yet committed this boot (`/run/boat-ab/committed`), or
       not committed in `grubenv`. The boot that committed its trial goes
       on updating; GRUB's last-resort boot may install, since that is how
       such a board is mended;
     - `/data` is mounted.
  2. **Fetch `index.p7`** with no redirects (`--max-redirs 0`), `https`
     only, `Cache-Control: no-cache`, and a 64 KiB size cap.
  3. **Verify it** (above).
  4. **Accept the index only if all of these hold:**
     - `channel` matches;
     - `serial` is above the stored maximum;
     - `compatible` matches;
     - `version` is newer than the booted slot (RAUC's relaxed-semver
       comparison, `reference.rst` "min-bundle-version");
     - `version` is not already in the other slot - counted only when a
       rollback would boot it: `<other>_OK=1`, and RAUC's last write there
       did not fail (RAUC records a failed write with the new version,
       status `failed`; a reflash leaves an old slot B in place, unusable).
       Otherwise the release is offered, and installed, again. Only
       `rauc status --detailed --output-format=json` reports it, as the
       slot's `slot_status.bundle.version`; plain `--output-format=json`
       leaves `slot_status` out. A slot that was flashed rather than
       installed has no bundle version, so for slot A as flashed the check
       reads the `/etc/boat-release` inside it with `debugfs`, not through
       a mount (a mount left behind by a stop or a busy mount point would
       make RAUC refuse that slot) - the same `BOAT_RELEASE` string a
       bundle carries;
     - `version` is not on the board's list of versions that **failed a
       trial here**.
  5. **Then the L4T check.** `l4t` must match the running
     `/etc/nv_tegra_release`. If it doesn't, report "BSP bump: reflash at
     the dock" - with `requires_reflash` or without, and not as an alert:
     while a fleet is reflashed boat by boat, every later release on the
     new L4T reaches the boats not yet reflashed this way. The same for a
     `firmware` that is not the board's `/usr/lib/boat-ab/firmware.id`
     (another kernel device tree on the same L4T, 5.9) - when both have
     one: a board without the record is offered the release.
  6. **Record** the accepted `serial`, plus the result and the channel's
     `published` date, for `status`.
- **`download`** writes to
  `/data/boat-update/downloads/<bundle>.part`:
  - **Resume:**
    - It resumes with `curl -C -`. Content-addressed names mean the bytes
      behind a name never change. A `.part` left from an older index is
      deleted.
    - Each run is a bounded window: `--max-time` of about 10 minutes, no
      `--retry` (a retry would reconnect over whatever link is now
      primary), and exit 28 treated as a normal end of window.
    - Between windows it re-checks the gates, the metered state and the
      policy.
  - **Size bounds:**
    - curl writes to a pipe and `boat-update` copies at most the index's
      `size` (64 KiB for the index) to disk, killing curl the moment a
      server sends more. curl 7.82 applies `--max-filesize` only to a
      `Content-Length`, which a chunked body does not have.
    - It treats a `.part` larger than `size`, or a resume answered with
      416, as corrupt and deletes it. A resume answered with the whole
      file (the server ignores Range) starts again from zero, in one window
      for the whole file, and counts as a re-fetch - so a server that keeps
      doing it cannot keep a board downloading for ever.
    - A complete download is recorded `ready` after RAUC has checked it; one
      completed but never recorded (a power cut in between) is picked up
      next time, not fetched again. An offer the channel withdraws takes
      its download, or partial download, with it; so does a new offer of
      another file (another name, or the same name with another SHA-256).
      Only a signed index withdraws: an index that is gone (404) or stale
      leaves a ready download in place, as an alert, and `install` warns
      that the channel may have withdrawn it. A board that never reaches
      the channel again installs what it has.
    - It keeps a `/data` free-space floor (bundle size + 2 GiB, because
      Docker shares `/data`) and re-checks it every window.
    - A bundle is **re-fetched from scratch at most 3 times**, then
      **given up** (by its SHA-256) until the channel publishes a new
      index. A transfer fault is never recorded as a failed trial.
  - **Redirects:** none (`--max-redirs 0`). If the host redirects, the URL
    in `channel.conf` must name the final location. A redirect, or any
    answer but the bundle (a 404, a 403), is an alert, and nothing of it
    is kept in the `.part`; a 5xx, 408 or 429 counts as a window without
    progress.
  - **Finish:**
    - At `size` it verifies `sha256`, renames the file, and runs `rauc info`
      on it (a local signature check), plus the `compatible` comparison,
      which `rauc info` does not do itself.
    - Only then is the state **"ready"**.
    - There is only ever one pending bundle.
  - **No sleep inhibitor.** A dropped link, an SC7 sleep or a reboot
    just means the next window resumes, and a background download must
    not keep the boat awake.
- **`install`** with no argument installs the ready bundle (5.6) as a
  transient systemd unit (`systemd-run`), so a dropped SSH session cannot
  kill the wrapper while RAUC carries on without its inhibitor.
  - After the update commits, the file is deleted; the old slot is the
    rollback, not the file.
  - If the trial fails, the version goes on the failed list and the file
    is deleted, never offered again as "ready".

**Scheduling** (review M5: monotonic timers pause during suspend, and
`Persistent=` only works with `OnCalendar=`):

- `boat-update-check.timer`: `OnCalendar=*-*-* 00/6:00`,
  `Persistent=true`, `RandomizedDelaySec=30min`.
- Also triggered from the system-sleep `post` hook and from a
  NetworkManager dispatcher script on `up`/`connectivity-change`.
- The service rate-limits itself with a stamp file on `/data`, so the extra
  triggers cost nothing. Only a check the channel answered stamps it (one
  the gates refused, or that got no answer at all, did nothing), and a
  stamp from the future does not count. Within the window an unfinished
  download (policy `download`) resumes without a check; nothing else
  runs. A check the clock gate
  refused runs again after 2 minutes (a transient timer), by when chrony
  has normally set the clock; each further refusal waits twice as long (4,
  8, 16, 32 minutes), at most five times a boot, then the timer's own
  schedule takes over.
- A command a person types does not give up on the timer's run, which can
  hold the lock for hours of downloading: it stops it (the download goes on
  from where it was at the next run) and waits for the lock.

**Configuration** (review M6) has two layers, and neither is carried over:

- `/etc/boat-update/channel.conf`, shipped by the image: defaults such as
  the channel URL. An update can change these.
- `/data/boat-update/channel.conf`, the operator's overrides, the
  optional client certificate/key and the optional channel CA. It is
  shared by both slots, so a credential rotated after an update is still
  right after a rollback. OTA needs `/data` anyway.

| knob | default | meaning |
|---|---|---|
| `BOAT_UPDATE_URL` | empty, or the build's `BOAT_UPDATE_URL` | channel base URL (final location; no redirects; no user name or password) |
| `BOAT_UPDATE_CHANNEL` | `stable` (`bench` in a `BOAT_BENCH` image), or the build's `BOAT_UPDATE_CHANNEL` | which channel to follow |
| `BOAT_UPDATE_AUTO` | `download` | `off` / `check` (notify only) / `download` (also fetch in background). **Never installs or reboots** (D12). |
| `BOAT_UPDATE_METERED` | `no` | download over a metered link? (D14) |
| `BOAT_UPDATE_RATE_LIMIT` | empty | curl `--limit-rate`. The stall threshold is fixed (`--speed-limit` 1 KiB/s for 120 s), so a rate limit must stay well above 1 KiB/s or every window ends as a stall |
| client cert/key | none | TLS client authentication (D15) |
| `BOAT_UPDATE_SERVER_CA` | `/data/boat-update/server-ca.pem` if present, else the image's `ca-certificates` | the CA the channel's TLS certificate must chain to (a bench server's own CA); for curl and the stream relay alike |

A check or download that fails for a reason other than the board's own
state (on trial, no network or no answer, the clock, another run) is kept
in `state.json` and shown
by `status` as an alert until a check works again, or for a download,
until the bundle is ready or the offer goes away (tests 34, 38).

**Metered detection** (review m1):

- The board takes the device the kernel routes the channel's address
  through (`ip route get`, so a VPN's split-tunnel route, a policy-routed
  WireGuard default and IPv6 count - not only the main table's default),
  uses that device's own NetworkManager `Metered` property, and binds curl
  and the relay to it (`--interface`, `SO_BINDTODEVICE`), so the bytes go
  where the decision was made.
- `yes`, `guess-yes` and `unknown` count as metered. `guess-no` counts as
  unmetered.
- NetworkManager guesses `yes` only for its own GSM/CDMA connections, and
  this image's NetworkManager is built without the ModemManager plugin, so
  it has none: a USB modem or a phone tethered over USB is Ethernet to it,
  `guess-no`. The board counts a device on a USB modem or tethering driver
  (`cdc_ether`, `cdc_ncm`, `qmi_wwan`, `rndis_host`, `ipheth`, ...) as
  metered unless its connection says `metered no`. **A 4G router reached
  over Ethernet or Wi-Fi reads `guess-no`**, so docs/07 tells you to mark
  that connection with `nmcli connection modify <c> connection.metered yes`.
- A VPN hides the underlying link. When the channel's route is a VPN, the
  board takes the conservative answer: metered if **any** active device is
  metered.
- While metered and not allowed, `check` still runs (it is tiny), and
  `status` says "update X available, waiting for an unmetered link".

**No `/data`, no OTA.** Downloads never go to the rootfs.

#### Streaming with adaptive updates (stage 2)

- **Bundles are adaptive from the start.** From Phase 3, the bundle recipe
  sets `RAUC_SLOT_rootfs[adaptive] = "block-hash-index"`, a manifest flag.
  The index adds about 0.8% of the *uncompressed* image and is harmless
  for stage 1.
- **`boat-update install --stream`** runs `rauc install` on a URL of a
  relay inside `boat-update` on `127.0.0.1`. `rauc install` waits for
  RAUC's `Completed` signal and fails if the service goes away mid-install.
  - **RAUC never talks to the channel.** Its streaming client follows
    redirects, to `http` too (`nbd.c`), and cannot be bound to a link. The
    relay fetches each of RAUC's Range requests from the verified URL the
    way `boat-update`'s downloads do: `https` only, no redirect followed,
    with the client certificate, and bound (`SO_BINDTODEVICE`) to the link
    checked when the stream started - not metered, unless `--allow-metered`
    or `BOAT_UPDATE_METERED=yes` allows it. Anything but a 206 of exactly
    the range asked for (same `Content-Range` offsets, that many bytes) is
    a 502 to RAUC, which fails the install; RAUC checks `Content-Range`
    only on its first request. A link that goes away fails it too. RAUC
    gets no TLS options, so the key stays root-only and nothing
    credential-bearing is on any command line. The relay serves only RAUC's
    streaming client: a socket held by its `rauc-stream` user (or root),
    from `/proc/net/tcp`, *and* by a process in `rauc.service`'s own
    cgroup, from the process's open files - a uid alone is not RAUC, since
    a host-networked container can run as root, and no container's process
    is in that cgroup, whatever namespaces it shares with the host. The URL
    also carries a token made for this stream. It serves only ranges up to
    64 MiB that end after they start, a few at a time. A connection from
    anyone else is closed as it is accepted, before it gets a thread; a
    refused request closes its connection, an idle one is closed after a
    minute, and only so many are held open - so no local process can starve
    RAUC of the relay. A VPN route to the channel is refused without
    `--allow-metered`: the tunnel follows whatever link carries it. With
    `server-ca.pem`, the relay trusts what curl trusts: an intermediate CA
    in it is a trust anchor.
  - The bundle URL is the one from the verified index, on the channel the
    offer was checked on. A preflight request for the first byte refuses a
    redirecting or Range-less channel before RAUC marks the slot
    unbootable. `boat-update` tells RAUC's pre-install handler which release
    it is installing (`/run/boat-update/expect`), so another signed bundle
    served at that URL is refused before anything is written; the installed
    release is checked against the index afterwards as well (5.7).
  - RAUC reads only the blocks it cannot find locally in either slot.
    RAUC's documentation reports **about 10% of the bundle** crossing the
    link for a small ext4 change.
  - Per-slot hash indices live in `/data/rauc`, built on the first run.
  - Data from an interrupted attempt is reused, so a retry costs less.
- **Needs** (review M2):
  - **NBD available without a manual `modprobe`.** OE4T's kernel builds
    it as a module, and RAUC finds it through a generic-netlink lookup
    that does not autoload modules. `boat-rauc.cfg` sets
    `CONFIG_BLK_DEV_NBD=y`; the fallback is `KERNEL_MODULE_AUTOLOAD += "nbd"`.
  - RAUC's `streaming` PACKAGECONFIG (meta-rauc's default).
  - The channel's Range support.
  - **A dedicated `[streaming] sandbox-user`**, so RAUC's streaming
    client does not run as root. It needs no credential: the relay holds
    the client certificate.
- **Limits on this BSP:**
  - kirkstone's libcurl has no HTTP/2, which RAUC recommends for
    high-latency links;
  - each request gets only 5 quick retries;
  - so a drop aborts the install safely (5.6) and it must be re-run.
- **When to use which:** download-then-install on shore Wi-Fi or any flaky
  link; streaming at anchor on a decent link. `status` shows the expected
  transfer for both.
- **Rejected: background streaming into the inactive slot.** RAUC's
  `activate-installed=false` would allow it, but RAUC leaves the target
  non-bootable until activation (`install.c`: "Leaving target slot
  non-bootable as requested by activate_installed == false"). The boat
  would have no fallback slot for as long as the activation waits for the
  operator. A downloaded file costs disk, not safety.

---

## 6. Implementation phases

Each phase ends with something testable on the bench. Nothing ships to the
boat before Phase 7 passes. OTA is part of the main line:

- **Stage 1** (download, then install) lands in Phase 5, as soon as there is
  a health gate to protect it.
- **Stage 2** (streaming plus adaptive updates) follows in Phase 6.
- **Phase 3 already builds bundles that are ready for both stages.**

**Phase 0: hardware spike (bench board, USB recovery at hand, no image changes).**
A "no" on any of these re-opens section 4. The tooling is
`scripts/ab-phase0-capture.sh`, which runs the read-only probe
(`scripts/ab-phase0-probe.sh`) on the board over SSH. It answers item 1,
and the parts of item 6 one snapshot can: NBD, the channel host's Range
support (`--channel-url`), and what NetworkManager reports as metered on
the links that are up. [The Phase 0 runbook](ab-phase0-runbook.md) covers
the steps that change state (items 2-5), each with how to undo it.

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
   - Is the Tegra WDT running during UEFI/GRUB? Test with a `grub.cfg`
     that sleeps - **once**: each watchdog reset is a warm boot that never
     reaches `nv_update_verifier`, so a test that repeats on every boot
     ends in NVIDIA's sticky state on the fourth (the runbook's 2.3 clears
     a flag before the test starts).
   - Is it running before userspace (`/sys/class/watchdog/watchdog0/state`
     early in boot; a one-shot boot with `rdinit=/bin/sh`)?
   - With `panic=10`, does `echo c > /proc/sysrq-trigger` reset the board?
     Does a hard hang?
4. **NVIDIA's counter:**
   - a cold power-on re-seeds the scratch register;
   - capture the UEFI-menu path that resets `RootfsStatusSlotA`, for the
     sticky-recovery procedure.
5. *(Optional, bench only.)* Confirm section 3 by switching to firmware
   chain B and watching it fail and revert.
6. **OTA prerequisites:**
   - The kernel has NBD (step 1). Whether it is a module or built in
     decides `boat-rauc.cfg`.
   - The host you intend to use for the channel answers the way RAUC
     probes it: a GET with `Range: bytes=0-3` returns `206` and a
     `Content-Range` with a numeric total, not `*`.
   - NetworkManager reports `Metered` correctly for the boat's cellular
     modem, for its Wi-Fi, and for any 4G router reached over Ethernet.

**Phase 1: layout, GRUB, slot-aware boot.** Effort: M. *Implemented; the
exit criteria wait for the bench board.*

- **Toggle.** `AB_LAYOUT` in `env.sh` (default **off** until Phase 7). For
  kas, a separate overlay `kas/ab-updates.yml` (static YAML can't read
  env.sh), plus an A/B block in `config/local.conf.sample`.
- **Build configuration** (`02-configure-build.sh` and the kas overlay):
  - `PARTITION_LAYOUT_EXTERNAL`;
  - the new sector expression;
  - `UBOOT_EXTLINUX = "1"`;
  - `IMAGE_ROOTFS_SIZE`.
- **`meta-boat` additions:**
  - a `tegra-bootfiles` bbappend that generates `boat-nvme-ab.xml` (pinned
    GPT numbers);
  - the `L4TConfiguration` overlay with GRUB mode;
  - `boat-bootmode-guard`;
  - `boat-grub-esp`: `grubaa64.efi` with `GRUB_BUILDIN` +=
    `regexp echo sleep reboot` (`loadenv` and `test` are already in
    OE-core's list), `grub.cfg` (generated and script-checked), and a
    pre-sized 1 KiB `grubenv`;
  - a `tegra-espimage` bbappend;
  - a `tegra-minimal-init` bbappend;
  - `grub-editenv` on the target;
  - a `grub-efi` bbappend whose patch re-arms the UEFI watchdog (300 s)
    where stock GRUB cancels it (5.1);
  - `boat-grubenv-guard`;
  - **a stub commit unit** that runs `grub-editenv … TRY=0 COMMITTED=1`.
    Without it, slot A would be marked bad on its second boot (review M10).
  - kernel fragment `boat-rauc.cfg` for whatever Phase 0 shows missing;
  - `boat-grow-rootfs` dropped from the A/B image, and refusing to run if
    `APP_b` exists (moved here from Phase 2: a Phase 1 board must not be
    able to run it).
- **Exit criteria:**
  - a fresh flash boots A through GRUB;
  - a test image `dd`'d into B boots via `grub-editenv`;
  - `sysrq-c` during a B trial lands on A;
  - **≥4 consecutive warm reboots on B** do not trip NVIDIA's counter;
  - removing `grubaa64.efi` still boots A via DIRECT;
  - a test `grub.cfg` that sleeps is reset by the UEFI watchdog within
    about 5 minutes - the one-shot test of the Phase 0 runbook (2.3), so the
    boot after the reset is a normal one.

**Phase 2: storage and state.** Effort: S–M. *Implemented; the exit
criteria wait for the bench board.*

- **`boat-storage`** (first boot, idempotent):
  - grows `DATA` and the flashed `APP` ext4 online;
  - runs `mkswap` on `SWAP` unless it already holds swap;
  - acts only on partition numbers whose GPT name matches, so stale data
    under `SWAP` from an older layout is fine.
- **Mount units**, `nofail`:
  - `DATA` → `/data`;
  - `SWAP`;
  - the bind mounts `/home/boat` (5.8) and the journal directory
    (`/var/volatile/log/journal`: with `VOLATILE_LOG_DIR`, `/var/log` is a
    symlink, and a mount unit must name the real path). An A/B build
    requires `VOLATILE_LOG_DIR` (poky's default): the stock
    `systemd-journal-flush.service` has `RequiresMountsFor=/var/log/journal`,
    which no drop-in can take away, and with a persistent `/var/log` that
    would make every boot wait for `DATA`'s filesystem check.
- **The `DATA` seed image** (`IMAGE_TEGRAFLASH_DATA`/`DATAFILE`), moved
  here from Phase 1 with the rest of `DATA`.
- **The ESP mount drop-in** (partition number, from Phase 1): verify its
  failure doesn't drop to emergency.
- **Exit:**
  - the first boot after a flash has `/data` filling the SSD, 8 GiB of
    swap, and `APP`'s filesystem filling its partition;
  - a reboot changes nothing;
  - pulling the `DATA` partition (deleting the GPT entry on a bench unit)
    still boots to a working desktop.

**Phase 3: RAUC.** Effort: M. *Implemented; the exit criteria and the
bundle sizes wait for the bench board.*

- **Layer plumbing:**
  - `01-fetch-layers.sh` + kas: add `meta-rauc` (kirkstone ships RAUC
    1.13);
  - bblayers in all three places, `LAYERRECOMMENDS_boat += "rauc"`;
  - appends under `layers/meta-boat/external/rauc/`;
  - `DISTRO_FEATURES += "rauc"`.
- **`rauc` bbappend:**
  - `system.conf`: `bootloader=grub`, `grubenv=` on the ESP, slots
    `rootfs.0`/`.1` as `/dev/nvme0n1p1`/`p2` with `bootname=A`/`B`, `type=ext4`,
    `resize=true`, `data-directory=/data/rauc`, keyring options from 5.7,
    `min-bundle-version`;
  - the root-only D-Bus policy;
  - a `rauc.service` drop-in.
- **Keys and bundle:**
  - `scripts/gen-update-keys.sh`;
  - `boat-bundle.bb`, with:
    - `inherit bundle` and `RAUC_BUNDLE_FORMAT = "verity"`;
    - `RAUC_SLOT_rootfs[adaptive] = "block-hash-index"` (a manifest
      flag), so every bundle from now on is stream-ready for Phase 6;
    - `RAUC_BUNDLE_VERSION` taken from the same `BOAT_RELEASE` string the
      image writes to `/etc/boat-release`, so a flashed slot and a
      bundle-installed slot report versions the same way;
    - a `[meta.boat]` section with `l4t` and `docker`;
    - a check that `/boot/Image` and `/boot/initrd` exist.
  - Record each bundle's size. These are the real numbers behind the OTA
    bandwidth estimates (D14).
  - RAUC built with its default `network` + `streaming` PACKAGECONFIG.
  - `CONFIG_BLK_DEV_NBD=y` in `boat-rauc.cfg`. Nothing autoloads the module
    RAUC looks for, so this is the fallback-free choice. With it, Phase 6
    needs no image rebuild just to enable streaming.
  - A `[streaming] sandbox-user` (`rauc-stream`) in `system.conf`.
- **Handlers:**
  - system `pre-install` (BSP/Docker checks, `COMMITTED=0` for the target);
  - the carry-over, as a `slot-post-install` hook inside the bundle (5.8);
  - system `post-install` (history).
- **Exit:**
  - `rauc install` writes B, the reboot is a trial, and it commits;
  - `rauc status` is right on both slots;
  - an unprivileged `busctl call` to RAUC is denied.

**Phase 4: health gate and reset coverage.** Effort: M. *Implemented; the
exit criteria wait for the bench board.*

- `boat-commit` (Tier 1/2, configured in `/etc/boat-update/health.conf`),
  replacing the Phase 1 stub.
- `boat-trial-deadline.timer`.
- Emergency/rescue drop-ins.
- Watchdog consolidation (D8), re-tested across SC7 because it touches
  `boat-power`'s sleep hook.
- The inhibitor held until commit.
- **Exit:** every row of 5.5 except firmware has been exercised once.

**Phase 5: OTA stage 1 (check, download, install) and operator tooling.** Effort: M.
*Implemented; the exit criteria wait for the bench board.*

- **`boat-update` (on the board).** `status` is the default. It changes
  nothing, except that run as root it records a trial that ended without a
  verdict, if nothing has yet (as `history` does). Without root, RAUC does
  not answer it (5.7), so the other slot's release is unknown and it says
  so. It shows:
  - the booted slot and whether it is a trial or committed;
  - the last result and its reason;
  - both slots' versions;
  - "booted outside GRUB" and `grubenv` alerts;
  - **OTA state:** channel, last check and its age, the channel's publish
    date, available version, download progress or "ready", metered/waiting,
    and "BSP bump: reflash needed".

  Verbs:
  - `check`, `download` and `install [--reboot]` (5.10, 5.6), sharing one
    `flock`;
  - `rollback`, `history`, and `debug-hold` (sets `BOAT_STAY`).
- **Scheduling:** `boat-update-check.timer` (`OnCalendar`,
  `Persistent=true`) and its service. It is also triggered from the
  system-sleep `post` hook and a NetworkManager dispatcher script, and
  rate-limited by a stamp file on `/data`.
- **Configuration:** the policy knobs in `/etc/boat-update/channel.conf`
  (image defaults) and `/data/boat-update/channel.conf` (operator
  overrides) (5.10).
- **Board-side state on `/data`:** the highest accepted index `serial` per
  channel; the versions that failed a trial here; the per-bundle re-fetch
  counter and the bundles given up.
- **Tools** (`openssl` and `curl`) are already on the image: `openssl-bin`
  comes in via `ca-certificates`, and `curl` is in
  `packagegroup-boat-nettools`.
- **`boat-update` is Python 3**, like `boat-sleep-listener`, so it adds no
  interpreter to the image. The index rules, version comparison and
  download bounds are clearer, and testable on the host, there than in
  `sh`; `openssl` and `curl` still do the cryptography and the transfer.
- **Keys:** `scripts/gen-update-keys.sh` also creates the index-signing
  leaf (emailProtection EKU only), and keeps the `serial` counter next to
  the keys.
- **Host scripts:**
  - **`scripts/publish-update.sh <channel> <bundle>`**: content-addressed
    bundle name; write `index.json`; sign it into one attached `index.p7`;
    upload the bundle first and the index last (5.10).
  - **`scripts/push-update.sh <bundle.raucb> [user@host]`**, the no-internet fallback:
    copy to `/data` (over ssh, so `BOAT_SSH_OPTS` stays ssh's), install,
    reboot, then *wait for a new boot ID over SSH and report which slot
    came back*; the copy is removed whatever happens. As root, or as
    `boat` through its passwordless sudo.
- **A bench update channel:** any local HTTPS server with Range support
  (e.g. Caddy) serving `bench` to the bench board (a `BOAT_BENCH` image),
  or `testing` to a board running a boat image. A LAN server's own TLS CA
  goes on the board as `/data/boat-update/server-ca.pem` (5.10).
- **Exit:**
  - a bundle published to the board's channel is downloaded in the background,
    survives a pulled Ethernet cable, an SC7 sleep and a reboot
    mid-download, and becomes "ready";
  - `boat-update install --reboot` takes it through trial to commit;
  - with the connection marked metered, it waits instead of downloading;
  - an index signed by a public CA certificate, a replayed older index,
    and a `testing` index served at `stable` are all rejected.
- **`docs/07-updates.md`**: day-to-day OTA, channel setup and publishing,
  fallbacks, rollback, recovery. Plus fixes to:
  - docs/01 (boot flow) and docs/04 (layout, flash; the first A/B flash
    erases the SSD);
  - docs/04 `--host-drive`: `doexternal.sh` isn't shipped by meta-tegra
    kirkstone, so A/B is `initrd-flash` only;
  - docs/05 "Updates" and "Reclaiming the rest of the SSD";
  - docs/06 troubleshooting, including the sticky state (a reflash until
    Phase 0 finds the UEFI-menu path);
  - README.

**Phase 6: OTA stage 2 (streaming + adaptive installs).** Effort: S–M.
*Implemented; the measurements and exit criteria wait for the bench board.*

- **`boat-update install --stream`:** `rauc install` through `boat-update`'s
  local relay, which fetches from the verified URL with no redirects, the
  client certificate and only over the checked link (5.10); never a header
  credential. Only an offer a download would take is streamed (newer than
  the running release, not failed here, checked on this channel), and
  afterwards the slot must hold the release the index named, or it is
  marked bad.
- **The `rauc-stream` sandbox user**, with no credential of its own.
- **Optional:** `rauc info <URL>` as a second, RAUC-native authenticity
  check before a streamed install. Not taken: it would check a different
  fetch than the install's. RAUC checks the bundle's signature before it
  writes a block, and the release check after the install ties the slot
  to the index.
- **Measure on the bench** the bytes transferred for:
  - a one-package change;
  - a kernel bump;
  - a CUDA bump;
  - a retry after a link drop mid-install.

  Put the numbers in docs/07, so choosing stream versus download is based
  on data rather than on RAUC's ~10% rule of thumb.
- **`status`** shows the estimated transfer for each path. Until the bench
  numbers exist, it gives the download's remaining size and says a stream
  fetches only the blocks neither slot has.
- **Exit:**
  - from a cold boot with no manual `modprobe`, `rauc info https://…` and a
    streamed, adaptive install both work, with and without a client
    certificate;
  - the client certificate never appears in the journal or in `ps`;
  - a link drop mid-stream leaves the board on its current slot with the
    target `OK=0`, and a retry succeeds;
  - a server without Range support fails cleanly, with a message pointing
    at the download path.

**Phase 7: validation.** Effort: M, mostly bench time. *The tooling is
implemented (fault bundles, `scripts/ab-soak.sh`, and the runbook in
[`ab-validation.md`](ab-validation.md)); the bench runs remain.*

- Run the section 7 matrix.
- Soak: 50 update cycles **delivered over the air** from the `bench`
  channel (`ab-soak.sh --ota`), alternating good and faulty bundles, plus
  random power cuts if a USB relay on the barrel jack is available.
- Then flip `AB_LAYOUT` to default-on and move the boat to `stable`.

**Phase 8 (later, separate plans):**

- Secure Boot, dm-verity and a read-only rootfs;
- credential lockdown;
- option B on an eMMC module;
- rauc-hawkbit-updater, if this ever grows to more than one boat;
- a USB flash that leaves `DATA` as it is (5.9).

---

## 7. Test plan: the acceptance matrix

All tests run on hardware, because QEMU cannot emulate Tegra UEFI. Two
pieces also run off-target:

- **GRUB state machine:** `scripts/test-grub-ab.py` runs the shipped
  `grub.cfg` in `grub-emu`, GRUB's own user-space build, from a GPT disk
  image with a FAT ESP, so `load_env` and `save_env` work on a real
  `grubenv`. It asserts the slot picked, the kernel command line and the
  `grubenv` written back for every row of 5.3's table, plus a torn or
  missing `grubenv`, an ESP that refuses writes, a kernel, `initrd` or
  `boot` that fails (also where that cannot be recorded), a trial in
  either slot, every last-resort order, and - starting from the
  config built into `grubaa64.efi` - a missing or truncated `grub.cfg`. (`qemu-system-aarch64` + AAVMF was the first idea; `grub-emu` runs
  the same script without a firmware image, and fits in CI. It passes with
  the board's GRUB 2.06 as well as the 2.12 CI installs.)
- **Shell logic:** `boat-update`, the handlers, `boat-storage` and the
  boot-state scripts get host-side tests with `rauc`, `grub-editenv`,
  `blkid` and `nmcli` mocked. `scripts/test-ab-scripts.py` runs
  `boat-bootmode-guard`, `boat-grubenv-guard`, the commit step, the
  `grub-editenv` RAUC runs and the RAUC handlers as shipped, against a scratch
  directory standing in for the board (`/proc`, an ESP with a real
  `grubenv`, the GPT names). `scripts/test-boat-update.py` runs
  `boat-update` against a local HTTPS channel.
  The OTA `check`/`download` logic also runs against a local Range-capable
  HTTP server, with cut-offs injected mid-transfer and a tampered index.
  `scripts/test-boat-storage.py` runs `boat-storage` against a fake board
  and checks every command it would run on SWAP and DATA, and the start
  order of the storage units (and RAUC's drop-in) as the recipe installs
  them.
  `scripts/test-ab-initramfs.py` runs the initramfs's root selection, and
  `scripts/test-ab-layout.py` the layout generator on a template in
  NVIDIA's shape (read back through meta-tegra's `nvflashxmlparse` when
  meta-tegra is checked out).

All of them run from `lint.sh`, so they cover every PR.

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
| 15 | trial: emergency mode; and a committed slot whose `local-fs.target` fails (a `Required` mount that fails) | reboot after 60 s → old slot; the committed slot's attempt count is not reset in emergency mode, so after two such boots GRUB falls back |
| 16 | trial: Tier 2 failure (`NetworkManager` masked) | mark-bad + reboot → old slot, reason recorded |
| 17 | trial: hard hang | WDT reset → old slot |
| 18 | trial: commit unit hangs | deadline timer → old slot |
| 19 | bundle whose rootfs lacks `/boot/Image` | rejected at bundle build; if forced, GRUB marks the slot and reboots |
| 20 | committed slot, `/data` missing | stays up (Tier 1 ok); Docker degraded; alert |
| 21 | ESP `grubenv` corrupted | boots by defaults; the guard rebuilds the file and alerts; next install works |
| 22 | `L4TDefaultBootMode` reset to 1 (UEFI defaults) | the guard restores it; that boot is flagged "outside GRUB"; install refused until rebooted via GRUB |
| 23 | `grubaa64.efi` missing | DIRECT boots A; flagged, with an alert that GRUB did not load |
| 24 | a JetPack SD card in the module slot, and a UEFI-bootable USB stick (JetPack-flashed: `APP`/`esp` labels and its own L4TLauncher), inserted before a reboot | ignored: UEFI boots the NVMe first (5.1), and root, ESP and RAUC slots resolve to NVMe partition numbers whose GPT names are checked |
| 25 | unprivileged user, and a container running as a non-root user with the system bus mounted, call RAUC `Install`/`Mark` | denied |
| 26 | `boat-update rollback` after commit | other slot boots and commits |
| 27 | `boat-sleep` (and the UDP sleep listener), and root's own `systemctl suspend`, during install and during a trial | refused (`boat-sleep --status` says why) |
| 28 | both slots broken | A, A, B, then UEFI stop (sticky recovery) is the documented end state; the USB reflash recovers it, and so does the UEFI-menu path once Phase 0 has found it and docs/06 describes it (open until then) |
| 29 | carry-over | SSH host key, `/home/root/.ssh`, `/home`, Wi-Fi, BT pairing, hostname, `boat-sleep.key`, passwords (D5), as they were before the update, are unchanged after the update and after a rollback. A password changed and a root key removed after the update are back after a rollback (documented; `rollback` warns). A Wi-Fi network added after the update, then a rollback and the next install: the network is missing on the old slot (documented), and the install keeps it in `/data/boat-update/displaced/` with an alert; an ordinary forward update keeps nothing and raises no alert |
| 30 | SC7 + WoL on both slots | works |
| 31 | OTA: channel offers a newer bundle, unmetered link | background download; `status` shows "ready"; no install or reboot happens by itself |
| 32 | OTA: link dropped, SC7 sleep, and reboot, each mid-download | resumes from where it stopped; final SHA-256 matches; never a second full download |
| 33 | OTA: truncated or corrupted download | SHA-256/`rauc info` mismatch → discarded and re-fetched; never offered for install |
| 34 | OTA: index with a bad signature, signed by the bench CA on a boat image, or signed by a public CA certificate | ignored before any download; alert in `status` ("the last check failed"), cleared by the next good check |
| 35 | OTA: index offers an older version, the same version, the version already in the other slot, a version that failed its trial here, another `compatible`, or another L4T | nothing downloaded; the L4T case reports "reflash needed" only for a newer version |
| 36 | OTA: cellular (metered) link - a USB modem, a tethered phone, or a router connection marked metered - default policy | check only; download waits for an unmetered link; moving to cellular mid-download pauses at the next window |
| 37 | OTA: clock at 1970 before NTP | check deferred, no TLS-error spam; runs after sync |
| 38 | OTA: `/data` too full, or not mounted | download refused, with the reason as an alert in `status`; rootfs never used. An install by any path (stream, push, USB) with `/data` at 0 bytes or 0 inodes is refused by the `pre-install` handler: the other slot's `grubenv` entries are unchanged |
| 39 | OTA: client-certificate channel | works for check, download and stream; the certificate is never in the journal or `ps`; still works after an update and after a rollback (it lives on `/data`) |
| 40 | OTA stream: adaptive install of a one-package change | commits; transferred bytes recorded (Phase 6) |
| 41 | OTA stream: link drop mid-install | install aborts; current slot unaffected, target `OK=0`; retry succeeds |
| 42 | OTA stream: server without Range support | clean failure pointing to the download path |
| 43 | OTA: replayed older index (lower `serial`), and a `testing` index served at the `stable` URL | rejected |
| 44 | OTA: a publish caught halfway (bundle uploaded, index not yet) | the board keeps the previous index; no false "bad signature" alarm |
| 45 | OTA: the index-signing certificate past its expiry date | still accepted (`-no_check_time`, as RAUC does for bundles); revocation is by removing the CA |
| 46 | OTA: the server streams an endless body, or more than `size` | aborted at `size`; the `/data` free-space floor is never crossed |
| 47 | OTA: a redirect to another host or to `http` | refused; no credential leaves the board |
| 48 | OTA: two downloads started at once (timer + manual) | the manual one stops the timer's, which resumes at its next run, and goes ahead; two from the timer: the second exits |
| 49 | OTA: checks across SC7 sleeps and after a link comes up | a check runs on resume and on link-up; at most one per rate-limit window (within it only an unfinished download resumes) |
| 50 | OTA: check and download during a trial boot | neither runs until the slot is committed |
| 51 | OTA: a 4G router on Ethernet marked metered in NetworkManager | treated as metered |
| 52 | OTA stream after a cold boot, with no manual `modprobe` | works (NBD built in) |
| 53 | trial: the helm's power-off (or the power key) before the commit, then again in the retried trial | the first time the trial runs again on the next boot; the second time it counts, and the board falls back |
| 54 | `grub.cfg` removed from the ESP | GRUB resets instead of stopping at a shell; repeated, that ends in NVIDIA's recovery state, as in row 28 |

The fault bundles for 13, 15, 16 and 18 come from a `boat-image-faulty`
variant (`BOAT_FAULT=<kind>`), signed by the bench CA (5.7); 12, 14, 17 and
19 are manual steps (`docs/plans/ab-validation.md`). A `BOAT_BENCH` build
has its own `compatible` (`…-bench`), so nothing built with the bench CA in
its keyring installs on a boat, and `publish-update.sh` sends it only to a
`bench` channel.

---

## 8. Migrating existing boards

The layout changes, and `APP` currently spans the SSD, so **the first A/B
install is a one-time full USB reflash that erases the SSD**. There is no
safe in-place conversion, and the plan does not attempt one.

1. On the old system, back up `/data` (compose, volumes, Signal K state),
   `/home` (which holds `root`'s home, `/home/root`, and its `.ssh`) and
   any hand-edited `/etc` files to a laptop with `rsync`. Copy Docker's
   data root, if at all, as root with `dockerd` stopped and `rsync -aHAX
   --numeric-ids` (overlay whiteouts are device files, opaque directories
   are `trusted.*` xattrs); re-pulling the images and copying only the
   volumes is simpler.
2. Full flash with `AB_LAYOUT=1`. The ESP lives in the external layout, so
   even `--skip-bootloader` rewrites it. A full flash is still recommended
   for the first A/B install, so that the GRUB-mode `L4TConfiguration`
   overlay lands in QSPI. (`boat-bootmode-guard` would fix the variable at
   runtime regardless.)
3. First boot: `boat-storage` provisions `/data`, swap and the rootfs size.
   Restore only what survives an update: `/data`'s app data, `/home/boat`
   (on `/data` now), and what 5.8 carries at every install - `/home/root/.ssh`,
   the Wi-Fi connections, the operator-edited `CONFFILES`. Other hand-edited
   `/etc` files and the rest of `/home/root` live in the slot and are gone
   after the first install (5.8): move those settings into `meta-boat` (or
   onto `/data`) before this migration, not back into the slot.
4. Put any channel overrides (and the client certificate, if you use one) in
   `/data/boat-update/`. Then prove the round trip before the boat leaves
   the dock: install a bundle once (A→B) and once more (B→A). The same
   bundle twice works only by file (`scripts/push-update.sh`, or `sudo
   boat-update install <file>`): over the air a board takes only a release
   newer than the one it runs and not already in the other slot, so an
   over-the-air round trip needs two successively newer releases, published
   on a channel of the board's own (`testing`, set in
   `/data/boat-update/channel.conf`) rather than to the fleet. After this,
   the only reasons to bring a laptop to the boat are a BSP bump or a dead
   board.

---

## 9. Risks and open questions

| risk | likelihood / impact | mitigation |
|---|---|---|
| GRUB on this UEFI mishandles the DTB, initrd or cmdline (SC7, display) | M / H | Phase 0 step 2 first; option C is the fallback |
| a GRUB-stage hang with no watchdog running | L / H | GRUB patched to keep the UEFI watchdog armed (5.1); Phase 1 exit test; Tegra WDT as a second layer (Phase 0) |
| a GRUB crash (an exception) with no watchdog running | L / H | the UEFI watchdog cannot fire then (5.1, 5.5): only the Tegra WDT, if Phase 0 shows it runs this early; otherwise a power cycle, after which a counted attempt falls back; a crash before `grub.cfg` saves the count repeats on both slots: a reflash |
| Tegra WDT not running in the early kernel | M / M | Phase 0 step 3; enable at probe; otherwise document power-cycle-to-recover for a kernel hang before systemd starts |
| NVIDIA sticky recovery reached (both slots fail, or a shared fault that slipped past `nofail`) | L / H | the 2+1 attempt budget (5.3); `nofail` shared mounts; a USB reflash today, a UEFI-menu reset once Phase 0 finds it; test 28 |
| `nvbootctrl verify` doesn't reset the count when booted from `APP_b` | M / H | Phase 1 exit criterion; if it fails, `boat-commit` resets it another way, or the budget becomes 1+1 |
| `grubenv` torn on vfat | L / M | guard unit, `.bak`, defaults (5.3) |
| ESP (vfat) damaged | L / H | rare, tiny writes; `fsck.fat` reported in `status`; recovery = USB reflash |
| a shared `/data` written by a newer slot breaks an older one | L / M | Docker-version warning (an alert, and in the install's history line); Docker stays on the kirkstone major. A systemd from 252 on writes compact journal files v250 cannot read: a release that moves Yocto branch sets `SYSTEMD_JOURNAL_COMPACT=0` until both slots read them |
| bundle size over cellular (several GB with CUDA) | H / M | metered policy (no background downloads on cellular by default); adaptive streaming (Phase 6); real sizes measured in Phases 3 and 6 |
| a streamed install fails repeatedly on a weak link | M / L | safe by construction (5.6); `status` steers to download-then-install |
| update channel down, or frozen by an attacker | L / M | the board simply stays on what it runs; channel age shown in `status` (5.7); SSH/USB fallbacks |
| index verification subtly wrong (OpenSSL purpose, trust store, binary mode) | M / H | exact flags pinned in 5.10, including `-no-CApath -no-CAstore`; tests 34, 43–45; host-side tests run the same `openssl` commands |
| channel credential leaks (redirects, logs, `ps`) | M / M | client certificate only, no redirects - streams too, through `boat-update`'s relay; tests 39, 47 |
| channel host lacks Range support, or per-file size limits (e.g. GitHub Releases) | M / M | Phase 0 step 6 checks the host; D13 |
| background downloads fill `/data` | L / M | free-space check, one pending bundle, deleted after commit |
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
| D7 | Transport | **OTA** from an HTTPS channel: download-then-install (Phase 5), then streaming + adaptive (Phase 6). SSH push and USB (exFAT/ext4) stay as fallbacks. |
| D8 | Watchdog: systemd `RuntimeWatchdogSec` as the only `/dev/watchdog` opener? | **yes** (the `watchdog` daemon's custom checks aren't used) |
| D9 | Key custody: dev keys outside `WORKROOT` with backup; production CA offline; bench CA separate | **yes** |
| D10 | Attempt budget: trial 1, committed 2 | **yes** (bounded by NVIDIA's 3-warm-boot limit) |
| D11 | `/home` on `/data` (persistent across updates) vs carry-over of `~/.ssh` only | **`/home` on `/data`**: the login users' homes; `root`'s stays in the slot (5.8) |
| D12 | How automatic is OTA? Check + download in the background, with install and reboot always started by you? | **yes** (`BOAT_UPDATE_AUTO=download`) |
| D13 | Where to host the channel: your own HTTPS server or object storage (needs Range requests and per-file sizes of several GB) | **your own host or object storage**, not GitHub Releases |
| D14 | Background downloads over metered (cellular) links | **no** by default; streaming at anchor when you choose to |
| D15 | Channel access: public, or a TLS client certificate? (Basic auth and bearer headers are ruled out, 5.10) | **public** to start; a client certificate if the host must stay private |

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
| M5 | major | Partition labels collide with JetPack SD cards and USB media (initramfs `blkid -l`, `boot-efi.mount`, RAUC) | NVMe partition numbers with a GPT-name check everywhere (fixed PARTUUIDs in v4; see pass 4); initramfs accepts only the two slots and drops the `mmcblk0p1` fallback (5.2); test 24 |
| M6 | major | RAUC's stock D-Bus policy lets anyone install or mark slots, and containers get the system bus | root-only policy (5.7); test 25 |
| M7 | major | The trial marker was written after RAUC activates the slot | inverted: `COMMITTED=0` is set in **pre-install**, so a fresh slot is a trial by default (5.3, 5.6); test 9 |
| M8 | major | Carry-over missed `root`'s `.ssh` (`/home/root/.ssh`) and `/home`, so key-only SSH would lock you out after an update | `/home/root/.ssh` carried; `/home/boat` on `/data` (D11) |
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

### Revision v3 (your request: OTA in the main phases)

| change | where |
|---|---|
| OTA is now a goal, and installs stay operator-started (D12) | 1 |
| New section on the update channel: signed index; check → resumable download → ready; metered policy; streaming + adaptive; background streaming rejected because it would drop the fallback slot (verified: RAUC leaves the target non-bootable when `activate-installed=false`) | 5.10 |
| The pull-only OTA threat model: freeze, bandwidth, privacy, credentials | 5.7 |
| Channel config split between image defaults and `/data` overrides, superseded in review pass 3 | 5.8, 5.10 |
| Phase 3 builds adaptive, stream-ready bundles; Phase 5 = OTA stage 1; Phase 6 = OTA stage 2; validation moves to Phase 7, later work to Phase 8; Phase 0 gains the OTA prerequisites | 6 |
| Tests 31–42 | 7 |
| Over-the-air round trip in migration | 8 |
| OTA risks | 9 |
| D7 rewritten; D12–D14 added | 10 |

### Review pass 2 (CodeRabbit, on v3)

| finding | resolution |
|---|---|
| A GRUB hang is covered only if the Tegra WDT runs that early, so "every failure ends in a reset" was not established | `grub-efi` patch re-arms the UEFI watchdog (300 s) instead of cancelling it; Phase 1 exit test; the claim narrows if the firmware ignores it (5.1, 5.5, 9) |

### Review pass 3 (independent adversarial review of the v3 OTA additions)

| id | severity | finding | resolution |
|---|---|---|---|
| B1 | blocker | `openssl cms -verify` with the codeSigning certificate never passes (default purpose `smime_sign`, no codesign purpose in OpenSSL 3.0). The obvious `-purpose any` also trusts `/etc/ssl/certs`, so any public certificate could sign an index. `-binary` mismatch, time checks and non-atomic `.sig` too. | separate index-signing leaf (emailProtection EKU), one attached `index.p7`, verify flags pinned (`-no-CApath -no-CAstore -purpose smimesign -binary -no_check_time`) (5.10) |
| M1 | major | replay and version holes: no `channel` or `serial`; re-offers the version in the other slot or one that just failed; reflash evaluated for old versions | signed `channel` + monotonic `serial`; compare against both slots and the failed list; reflash only for newer versions; `/etc/boat-release` = `BOAT_RELEASE` (5.10, Phase 3) |
| M2 | major | NBD is a module nothing loads; the streaming sandbox can't read a root-only TLS key | `CONFIG_BLK_DEV_NBD=y`; `rauc-stream` sandbox user with a group-readable key (superseded in Phase 6: RAUC streams through `boat-update`'s relay, which holds the credentials, so the key stays root-only and the sandbox needs none); cold-boot exit test (5.10, Phases 3/6) |
| M3 | major | credentials leak via redirects (also to `http`), RAUC's header logging and `ps` | client certificate only; no redirects; D-Bus `InstallBundle` (5.7, 5.10, D15) |
| M4 | major | resume splices a republished same-name file; 416 exits 0; unbounded body; endless re-fetch; no lock | content-addressed names; size and free-space bounds every window; 3 re-fetches max; `flock` (5.10) |
| M5 | major | monotonic timers pause in suspend; `Persistent=` needs `OnCalendar=` | `OnCalendar` + `Persistent`; triggers from sleep `post` and NM dispatcher; stamp-file rate limit (5.10) |
| M6 | major | a carried-over `channel.conf` goes stale after a credential change and blocks image defaults | image defaults in `/etc`, operator overrides on `/data`; dropped from carry-over (5.8, 5.10) |
| — | minor | metered detection (per-device, `--interface`, 4G router over Ethernet, VPN); no OTA during a trial; `rauc info` checks only the signature; streaming limits (no HTTP/2); index field validation; inhibitor tied to the SSH session | all in 5.10 and the tests |
| — | nit | Range probe must be a GET with `Content-Range` total; adaptive is a manifest flag; clock gate on chrony sync only; `openssl` already on the image | fixed |

### Review pass 4 (findings while implementing Phase 1)

| finding | resolution |
|---|---|
| meta-tegra's `make-sdcard` reads `<unique_guid>` but never hands it to `sgdisk`, so fixed PARTUUIDs are random after every flash | partitions are addressed as `/dev/nvme0n1p<N>` with pinned GPT numbers, and every user checks the GPT name first (5.2, M5) |
| an entry appended after `APP_b` without an explicit id is numbered "previous id + 1" by `nvflashxmlparse`, colliding with the kernel partition | `SWAP`/`DATA` get explicit ids; the build verifies every pinned number (5.2) |
| `boat-grow-rootfs` on an A/B board would wreck `APP_b` | dropped from the A/B image and guarded in Phase 1, not Phase 2 |
| `loadenv`/`test` are already in OE-core's `GRUB_BUILDIN`; `halt` is never used | `GRUB_BUILDIN` += `regexp echo sleep reboot` |
| `tegra-bootfiles` has no unpack step, so a layout script listed in `SRC_URI` never reaches `WORKDIR` and the layout task never ran | the task runs the script from the layer, with a file checksum, after `do_configure` |
| replacing meta-tegra's `L4TConfiguration` overlay means carrying NVIDIA's other defaults too | `fdtput` rewrites only `L4TDefaultBootMode` in the built `.dtbo` (5.1) |
| `save_env` refuses GRUB's host filesystem in `grub-emu`, so a test against a plain directory proves nothing about the counting | the harness boots from a GPT image with a FAT ESP (7) |
| POSIX `sh` has no local variables: a `lib.sh` helper's `env` overwrote the guard's own, so a torn `grubenv` was "rebuilt" into a stray file and the good `grubenv.bak` overwritten (found by `test-ab-scripts.py` on its first run) | every helper variable carries an `_ab_` prefix; the harness covers the torn-`grubenv` paths |
| on vfat, a rename frees the replaced file's clusters at once but writes the moved file's directory entry only at writeback, so for ~30 s `grubenv` points at freed clusters: a power cut then undoes a commit, and `fsck` truncates both copies | the write fsyncs the renamed file itself before the directory |
| `boot-efi.mount`, which the ESP drop-in amends, only reaches an image through `tegra-redundant-boot` in `MACHINE_EXTRA_RDEPENDS` | `boat-ab-boot` depends on `setup-nv-boot-control-service` itself |
| `grub-emu` has every module; the board's GRUB has only `GRUB_BUILDIN`, and a missing command only prints an error, so the harness could not see one (`true` was missing) | `test-grub-ab.py` checks every `grub.cfg` command against `GRUB_BUILDIN` and its dependencies; `true` added |

### Review pass 5 (findings while implementing Phase 2)

| finding | resolution |
|---|---|
| `initrd-flash` substitutes the `DATAFILE` placeholder, or with no data file deletes every line containing it (`sed '/DATAFILE/d'`); a layout that keeps several elements on one line would lose `SWAP`, `DATA` and the secondary GPT with it | the generated layout keeps one element per line, and the build fails if a placeholder shares its line |
| bind-mounting all of `/home` hides `root`'s home (`/home/root` on this distro), and its SSH keys with it whenever `DATA` fails | only the login users' homes move to `/data` (D11, 5.8) |
| with `VOLATILE_LOG_DIR`, `/var/log` is a symlink into a tmpfs, and a mount unit on a symlinked path never starts | the journal mount names the real directory |
| the board should never decide on its own to format a partition that might hold data | the flash writes a small, empty ext4 seed into `DATA`; the first boot only grows it, and `boat-storage format-data` is a manual, confirmed step |
| systemd's `gpt-auto-generator` enables any partition of the Linux swap type on the root disk - `SWAP` is one - without `boat-storage`'s checks, and races its `mkswap` on the first boot | `boat-storage` masks the unit it would generate (`dev-<disk>p<N>.swap`) |
| ordering `systemd-journal-flush` after the journal's bind mount made the whole boot, which waits for that unit, wait for `DATA`'s `e2fsck` - minutes for a repair on a large drive, which a time limit would then kill half way | the early flush is skipped unless the mount is already there; `boat-journal-flush.service`, pulled in by the mount, moves the journal onto `DATA`, and the check has no time limit |
| with the seed in every flash, a USB reflash also empties `/data` | stated in 5.9 and docs; a flash that keeps `DATA` is Phase 8 |
| a power cut just after seeding `/home/boat` could leave a home of empty files that is never seeded again | `sync` before and after the rename that makes it final |
| `scripts/test-ab-scripts.py` did not cover `boat-storage` | `scripts/test-boat-storage.py` runs it against a fake board: swap, the `DATA` check, seeding, `format-data`, `status` |

### Review pass 6 (findings while implementing Phase 3)

| finding | resolution |
|---|---|
| a build-date default for `BOAT_RELEASE` breaks bitbake: `DATE` is outside the parse-cache hash, so a task signature that depends on it fails on the first build of a day | an empty `BOAT_RELEASE` means `0.0.0-dev`; every development build reports that, so OTA never offers one over another |
| a carry-over in a system handler runs the *old* release's list, so a new release could never add a path to carry | the carry-over is a hook inside the bundle |
| `AB_LAYOUT=1` without `meta-rauc` in bblayers parses fine and builds an image that cannot be updated | the build stops with a fatal error naming the missing layer |
| a signing key generated into the repository or the work tree is one `git add` away from being published | `02-configure-build.sh` refuses such a `BOAT_KEYS_DIR`, and `lint.sh` fails on any tracked PEM private key |
| RAUC 1.13 runs the system `pre-install` handler *before* it checks `compatible` and `min-bundle-version`, so marking the target `OK=0` there disabled the fallback slot for a bundle RAUC then refused | the handler sets only `COMMITTED=0`; RAUC itself sets `OK=0` just before it writes (5.6) |
| system handlers get the `[meta.*]` keys but no `RAUC_MF_*`, so the history never had the version | the bundle carries its release as `[meta.boat] release` too |
| the password merge's `NR == FNR` treats an empty running `/etc/shadow` as the image's, and installed an empty one | the merge tells the files apart by name, and the hook refuses (failing the install) a result that lost lines or `root` |
| a carry-over copy that failed half way left the new slot with neither version | it copies beside the target and renames |
| `debugfs` is in the native sysroot's `/sbin`, not `/usr/sbin`, so the bundle could not build | found on `PATH` |
| `do_bundle`'s signature had the key's path, not its contents | `file-checksums` on the key, certificate and keyring |
| RAUC splits a version at its last `-`, so `1.0.0-rc-1` would not parse | `BOAT_RELEASE` allows no `-` inside the pre-release part |
| dbus-daemon's policy goes by uid, so a container running as root with the system bus passes the root-only policy | stated in the policy and 5.7: such a container must run as an unprivileged user |

### Review pass 7 (findings while implementing Phase 4)

| finding | resolution |
|---|---|
| a committed boot whose Tier 1 check fails, if it left its attempt count alone, would hand the boat to the older slot after one more unlucky boot, for a fault (an unmounted ESP, say) that the other slot shares | every committed boot that reaches `boat-commit` resets its count; a Tier 1 failure is an alert only (5.4) |
| the `watchdog` daemon and systemd's `RuntimeWatchdogSec` cannot both open `/dev/watchdog` | A/B images leave the daemon out (D8); `boat-power`'s watchdog setting has no effect there, and Phase 7 re-tests SC7 that way |
| an emergency-mode reboot with no delay gives nobody at the console the chance to read why | 60 s on the console first; `boat.stay=1` keeps the shell for bench work |
| the gate was ordered after Docker, which waits for `/data` and its unbounded check: a slow `/data` could fail a good trial at the deadline, or leave a committed slot's count unreset through two power cuts | not ordered after Docker (the checks retry for it); a committed boot resets its count before any check |
| Tier 1's `rauc status` goes through `rauc.service`, which waits for `/data` - so a `DATA` fault failed the OS's Tier 1 | checked only when `/data` is the `DATA` partition |
| `grubenv_set` returned only its last step's status, so a commit whose write failed reported success (and the deadline then rolled it back without a word) | every step must succeed; the commit reads `COMMITTED=1` back |
| `systemctl suspend` as root ignores block inhibitors unless asked to check them, so `boat-sleep` ignored the trial's inhibitor (and every other one) | `--check-inhibitors=yes`; and `boat-sleep` refuses during a trial even with `--force` |
| RAUC's mark-good rewrites `grubenv` in place: a torn write leaves GRUB the `.bak` copy, where the passed trial still looks used up | commit and fail with one atomic `grubenv` write |
| the gate and the deadline could decide at the same time, and through the same temporary file | one lock; a commit stops the deadline timer; `BOAT_HEALTH_DEADLINE` is held within the unit's limit |
| the deadline took `COMMITTED=1` in `grubenv` as a commit, though a commit whose write failed after the rename (the flush, or `grubenv.bak`) reports "NOT committed" and may never be durable (CodeRabbit) | the commit records itself in `/run/boat-ab/committed` only once the whole write succeeded; the deadline requires that record and fails the trial without it |
| with `/data` missing, `last-result` and `failed-versions` went onto the slot's own rootfs under the bare mount point | written only when `/data` is the `DATA` partition |
| an operator's `health.conf` was replaced by the image's at every update | carried over (5.8) |

### Review pass 8 (findings while implementing Phase 5)

| finding | resolution |
|---|---|
| `install` re-runs itself as a systemd unit, so an outer run that held the update lock blocked its own inner run | only the inner run, the one that calls RAUC, takes the lock |
| the timer re-checks while a download is under way; a strict "serial must grow" rule rejected the unchanged index as a replay | the same serial is accepted when the index is byte-for-byte the one accepted before (its hash is stored); anything else must be newer |
| a downloaded bundle that RAUC refuses (wrong `compatible` or version) would be offered and fetched again forever | it is deleted and given up until the next index (a publishing fault is not a failed trial) |
| curl 7.82 applies `--max-filesize` only to a `Content-Length`: a chunked body from a hostile host filled `/tmp` with the "64 KiB" index and `/data` with a "3 MiB" bundle | `boat-update` copies curl's output itself, at most the signed size, and kills curl past it |
| transfer faults - a server ignoring Range, a 416, an oversize body - recorded the release as failing a trial, for good | they give up that bundle until a new index; a server that ignores Range restarts from zero, uncounted; re-fetches really are 3 |
| `install` exited 0 when the install unit failed (the lock was busy, or RAUC refused), so `push-update.sh` reported success | the unit keeps its result, the wrapper reads it, and it takes the lock first to fail fast |
| a missing or restored serial counter restarted at 1, so every board rejected every index as a replay; a channel switch could too | the serial is at least the time in seconds and recorded before the upload; boards keep one serial per channel |
| a ready bundle stayed installable after something newer arrived by push, or after a trial that died before `boat-commit` | only the current offer, newer than the running release and not failed, is installed; a trial that ended without a commit is recorded as failed |
| `status` mounted the other slot when RAUC was busy writing it | only `check`, under the update lock, mounts a slot |
| `debug-hold` wrote `grubenv` through the same temporary name as `boat-commit`, without its lock | it takes `boat-commit`'s lock and a unique temporary file, and writes durably on vfat |
| `-purpose smimesign` accepts a signer with no extended key usage | the signer must name emailProtection; serials are capped |
| index fields shown to the operator could carry control characters | refused (notes are filtered of C0, C1 and DEL) |
| the accepted index's hash was kept once for all channels: back on a channel whose index had not changed, the board cried replay, and dropped that channel's given-up and re-fetch state (CodeRabbit) | the hash is kept per channel, like the serial |
| `push-update.sh` ignored the install's exit status, so a refused install surfaced as "did not reboot within 5 minutes" (CodeRabbit) | only ssh's 255 - the connection dropped by the reboot - is the expected end; any other failure stops with the install's error |

### Review pass 9 (findings while implementing Phase 6)

| finding | resolution |
|---|---|
| `InstallBundle` returns as soon as the install starts, so its exit status says nothing about the result | `rauc install`, which waits for RAUC's `Completed` signal (below) |
| RAUC's streaming client drops to `rauc-stream` before it opens the client certificate and key, so root-only files fail the stream, not the check | RAUC no longer opens them: it streams through `boat-update`'s relay, which holds the certificate; the key stays root-only (below) |
| following `Operation` and `LastError` over `busctl` read a failed property read, and a RAUC that died mid-install (D-Bus activation restarts it idle, with no error), as a finished install - recorded, rebooted into, then recorded as a failed trial | `rauc install`: it subscribes to `Completed` before the call and fails when the service disappears |
| a stream installed whatever signed bundle the channel served: RAUC checks the signature, not that it is the release the index offered, and its streaming client follows redirects - an older, vulnerable release could be installed | after the stream, the slot must hold the index's release (RAUC's slot status), or it is marked bad (`ORDER` back, `OK=0`) before any reboot |
| a stream skipped the download path's checks: a failed release, or one no newer than the running slot (overwriting the rollback), was streamed; a streamed release's offer outlived its commit | a stream takes only an offer that is newer and not failed; housekeeping drops an offer that is neither |
| an install that failed left the previous install's record in place, so the slot it had marked bad was recorded as that release failing a trial | each install forgets the previous record before RAUC writes the slot |
| a stream fetched the offer's bundle by name from whatever channel was configured by then, though it was checked on another (CodeRabbit) | the offer records its channel and URL; a stream refuses an offer checked elsewhere |
| RAUC's streaming client follows redirects, to `http` too, so a channel could send the board's requests elsewhere - its client certificate to another host, or GETs into the boat's own network (CodeRabbit) | RAUC streams from `boat-update`'s relay on `127.0.0.1`, which follows no redirect and passes on nothing but a 206 of exactly the range asked for; a preflight also refuses a redirecting or Range-less channel before RAUC marks the slot unbootable |
| the metered-link check was made once, when a stream starts, and RAUC's client cannot be bound to a link: a route that moved to cellular mid-stream would be used (CodeRabbit) | the relay's connections are bound to the checked link (`SO_BINDTODEVICE`); if it goes away, the stream fails and the board stays on its slot |
| the relay passed on any 206 no longer than the range asked for, and RAUC checks `Content-Range` only on its first request: bytes from elsewhere in the bundle would be written at the offset RAUC asked for (CodeRabbit) | the relay passes on only a 206 whose `Content-Range` names the offsets asked for and whose body is exactly that long; anything else is a 502 |

### Review pass 10 (findings while implementing Phase 7)

| finding | resolution |
|---|---|
| a good bundle from a `BOAT_BENCH` build is fleet-signed but its rootfs trusts the bench CA (bundles and indexes), and nothing stopped it from being published to `testing` and promoted to `stable` | a bench build has its own `compatible` (`…-bench`), so boats refuse it in RAUC and in the index check; `publish-update.sh` sends bench bundles only to the `bench` channel; the build stops if the bench CA is the fleet CA |
| `ab-soak.sh` passed a faulty cycle whose bundle was refused before anything was written (same slot, committed), and stopped dead when SSH was down at the start of a cycle | every cycle must change the boot ID, a faulty one must leave the other slot `OK=0`; the bench image is required up front; an unreachable board fails that cycle, not the run; the copy goes over `ssh` (`BOAT_SSH_OPTS` is written for it) |
| each fault-bundle build replaced the previous one in the deploy directory, and the soak's globs matched every bundle | the runbook copies each bundle out under its own name as it is built |
| overwriting the start of a slot breaks its superblock, so GRUB fails the slot before the initramfs is ever reached (row 14 tested row 19's path) | row 14 damages the journal, which only the kernel reads; row 12 corrupts the kernel past its header |
| `ab-soak.sh` only installed over SSH, so the Phase 7 target - 50 cycles delivered over the air - had no runnable procedure (CodeRabbit) | `ab-soak.sh --ota DIR [--stream]` publishes each cycle's bundle to `bench` and has the board check, download (or stream) and install it; the runbook builds the versioned bundles it needs; the CSV records the delivery, and each soak is its own result |
| a power cut mid-copy or mid-install left `ab-soak.sh` waiting on a dead SSH session for TCP's timeout (CodeRabbit) | SSH sessions send keepalives and end a minute after the board stops answering |
| a power-cut cycle could pass on the board's state from before a late cut, and a cut command that failed still passed (CodeRabbit) | the cycle waits for the cut first and judges the board after it; a failed cut command fails the cycle |
| a power-cut cycle passed when the update failed before the cut - the cut alone rebooted the board onto a committed slot; over the air, most cuts landed in the download (CodeRabbit) | `boat-update`'s own failure (exit 1-127) fails the cycle, unlike a session the board dropped (255, or a signal); over the air the check and download run first, and the cut is timed from the install |
| after a cut the board could already be back up, so the wait to see it go down ran its full ten minutes (CodeRabbit) | a cut cycle does not wait for the board to go down; its uptime must be shorter than the time since the cut, which proves it booted after it |

### Review pass 11 (internal multi-agent review of the implementation)

A review of the whole implementation by independent agents, each finding
checked by a second one against the code and the upstream sources. The
boot and fallback findings:

| finding | resolution |
|---|---|
| the last resort always booted `A`, and `boat-commit` then took it for a committed boot: a release that had failed its trial was made usable again, and a good committed `B` abandoned | committed slots first, in `ORDER`, then the others; a slot that will not load hands over; `boat-commit` alerts on every last-resort boot and never makes a slot usable that has not passed a trial (5.3, 5.4) |
| a clean shutdown or reboot from the helm during a trial failed it, and the release was never offered again; only sleep was held off | the trial gets its attempt back once, at shutdown (5.4); the skipper can still turn the computer off |
| the bundle hook carried `health.conf`, `daemon.json` and `/etc/default/*` whether or not anyone had edited them, so a release could never change their defaults - a renamed unit in the health gate's list would fail every trial | only files that differ from what the running image shipped are carried (5.8) |
| UEFI booted USB first (`DefaultBootPriority`, and `NewDeviceHierarchy` putting every new stick first), so a bootable stick left in replaced the whole A/B path; test 24 assumed otherwise | NVMe first and new devices last, in the overlay and at runtime (5.1); test 24 restated for a bootable stick |
| a torn in-place `grubenv` write can leave a valid-looking block with stale duplicate keys, where GRUB's attempt count never advances; RAUC wrote `grubenv` in place | RAUC writes through boat's `grub-editenv`, a locked copy and rename like every other writer (5.3); M2's "unreadable file" was only one of the outcomes |
| a missing or truncated `grub.cfg` left GRUB at a prompt (an open shell when missing) | the config built into `grubaa64.efi` locks the shell, runs `grub.cfg` and resets (5.1, 5.5) |
| `nv_update_verifier`, which the attempt budget depends on, reached an image only through `packagegroup-base` | `boat-ab-boot` depends on `tegra-redundant-boot` |
| `debug-hold` stayed on until someone turned it off, disabling every automatic reset | it ends with the first healthy boot that carried it (5.4) |
| a committed boot whose attempt reset failed (an ESP refusing writes) reported healthy, and a failed guard stopped `boat-commit` from running at all | the reset is an alert; the guard resets early, alerts when it fails, and `boat-commit` only `Wants=` it |
| an empty `RequiresMountsFor=` does not reset the list in systemd v250, so the journal-flush drop-in could not remove the stock unit's wait for `/var/log/journal` (pass 5's fix) | an A/B build requires `VOLATILE_LOG_DIR`, where that path names no mount (Phase 2) |
| the A/B image kept the single-slot 4 GiB `IMAGE_ROOTFS_EXTRA_SPACE`, capping a slot's content at about 9 GiB | 512 MiB on the A/B layout (5.2) |
| a bundle RAUC refuses after the pre-install handler (wrong `compatible`, below `min-bundle-version`) still demoted the fallback slot | the handler makes RAUC's two checks first (5.6) |
| `min-bundle-version` was not wired into any build script or document | `BOAT_MIN_BUNDLE_VERSION` in `env.sh`, `02-configure-build.sh` and the kas overlay, checked against `BOAT_RELEASE` as the build parses (5.7) |
| bench builds signed their good bundles with the fleet key, which then had to be wherever bench builds run | a bench build signs with the bench keys |
| `boat-sleep --force` slept during an install; and `--check-inhibitors=yes` refused whenever the helm user was logged in, on every build | refused during an install as during a trial; `boat-sleep` reads logind's list of sleep locks itself (logind never applies root's own locks to root), and then asks with logind's own check - neither refuses over a logged-in user (5.6) |
| the trial deadline after a `boat.stay=1` failure overwrote the gate's reason and listed the release as failed twice | the deadline leaves a slot that is already failed alone |
| plan 5.3 described a `grubenv` rebuild that consulted RAUC's status, which the guard never did | 5.3 describes the rules the guard implements, and what they mean for a rollback |
| the GRUB harness ran no committed-slot load failure, no failing `initrd` or `boot`, no last resort with `B` committed, no `grub.cfg` failure, on an 8 MiB FAT; the scripts harness had no last-resort, lock or `boat-bootmode-guard` case; the initramfs, `boat-storage grow` and the layout generator had no test | all added: `test-grub-ab.py` (64 MiB FAT32, the built-in config), `test-ab-scripts.py`, `test-boat-storage.py`, and the new `test-ab-initramfs.py` and `test-ab-layout.py` |

The update client and host tooling findings:

| finding | resolution |
|---|---|
| the boot that committed a trial refused every OTA verb and install until the next reboot (`boat.trial=1` stays on the command line) | "on trial" is one rule everywhere: booted as one and not yet committed this boot, or not committed in `grubenv`; a last-resort boot may install (5.10) |
| a failed slot write is recorded by RAUC with the new version, and a reflash leaves an old slot B in place: either way the release counted as "already in the other slot", which `rollback` then refused | the other slot's release counts only if a rollback would boot it (5.10) |
| a server that ignores Range sent a bundle larger than one window into an endless download loop | one window for the whole file, counted as a re-fetch (5.10) |
| the stream relay served any local process with the client certificate, and a reversed range made it read the whole bundle into memory | RAUC's streaming user and root only, and only from a process in `rauc.service`'s cgroup (a root process in a container is not RAUC, even one sharing the host's PID and network namespaces - CodeRabbit), on a URL with a per-stream token, valid ranges only, a few at a time (5.10) |
| `publish-update.sh` published the index when the bundle copy failed, and published any squashfs | every step stops the publish; the copy is checked, and a bundle given as a symlink (the deploy directory's link, which rsync would skip) is published as the file it points at; the bundle's signature, CA, key usage and verity format are checked first, and the index is made from the signed manifest |
| a streamed install checked the release only after RAUC had activated the slot | the pre-install handler refuses another release before anything is written (5.10) |
| `boat-update` did not depend on what `http.server` imports, or on chrony | `python3-netserver`, `python3-html`; chrony recommended, `timedatectl` when it is missing; the health gate fails a trial whose `boat-update` does not start |
| the rate-limit stamp was written before the clock gate, holding off checks for 30 minutes after a boot | stamped after the gates; a future stamp does not count; a check the clock refused runs again shortly, at 2 to 32 minutes, five times a boot (5.10) |
| a trial that ended without a verdict left no reason, and a stale "committed" in `last-result` | `boat-update` records it, in `last-result` and history, as soon as anyone looks |
| the client key's mode was not checked | a key others can read is refused, with the fix |
| `push-update.sh` put the bundle's file name into a root shell command | only a plain bundle name is accepted (and in `ab-soak.sh`) |
| a stream over a VPN default route could move onto a metered link | refused without `--allow-metered` |
| a power cut or stop between finishing a download and recording it meant a full re-download; a withdrawn offer kept its download | picked up next time; withdrawn with the offer (5.10) |
| `boat-update download` exited 0 without a ready bundle | exit 2 |
| `rollback` went through `rauc.service`, which needs `/data` - a release that broke `/data` committed and could not be rolled back | `rollback` writes `grubenv` itself (5.6) |
| a background download held the lock for hours and refused every other command | a person's command stops it (a running oneshot is `activating`, which `is-active` does not count) and goes ahead; a stop unwinds, so the other slot is never left mounted; test 48 says so |
| a pre-install handler's refusal reached only `rauc.service`'s journal; the operator saw "exit 1" | `boat-update` shows the handler's lines from that journal |
| `ab-soak.sh` passed a power-cut cycle when the cut command cut nothing | a cut cycle installs without `--reboot`, so only the cut can reboot the board |
| a kas build skipped the release, key directory and bench checks, and a release that is not a version made boards take any offer as newer | the rauc bbappend checks them as the build parses; `boat-update` refuses to compare against a release that is not a version |
| the harness never ran the file install, the wrapper's exit status, `auto`, the trial gates, rollback, a failed write, 416, the free-space or RAUC refusals | all added to `test-boat-update.py`, and the new `test-publish-update.py` |
| CI ran the boat's Python under the runner's newer Python | `lint.sh` also compiles it and runs `test-boat-update.py` under 3.10 (CI installs it) |
| a channel whose TLS certificate comes from its own CA (a bench server) could not be used: the board trusted only the slot's store, and a CA added there was gone after the first update | `BOAT_UPDATE_SERVER_CA`, by default `/data/boat-update/server-ca.pem`, for curl and the relay (5.10) |
| tests 34 and 38 expected an alert in `status` that `boat-update` never showed: a refused check or download left only the journal | the last failure is kept and shown as an alert until it is resolved (5.10) |

The documentation and runbook findings:

| finding | resolution |
|---|---|
| the documented release flow - publish to `testing` for the bench, then the same bundle to `stable` - could not work once bench images got their own `compatible`: a bench board refuses every boat bundle, and a bench bundle never leaves `bench` | `testing` is followed by a test board running a boat image, and the bundle that committed there is the one promoted; `bench` is the validation bench's own channel (5.10, docs/07, `publish-update.sh`, `channel.conf`) |
| Phase 0's watchdog test - a `grub.cfg` that only sleeps, or `init=/bin/sh` - repeats on every watchdog reset and reaches NVIDIA's sticky state on the fourth boot, with no undo | a one-shot test that clears its flag before it starts, `rdinit=/bin/sh` for the early-boot case, and an undo (runbook 2.3, Phase 0 and Phase 1 exit) |
| the bench runbook said `BOAT_BENCH` did not outlive its subshell; it stays in `local.conf`, so a later boat build was a bench image | said so, and `03-build.sh` refuses a bench-configured build directory unless `BOAT_BENCH=1` |
| the bench channel had no TLS trust path the board could use, and its check ran from the host with the host's trust store | `server-ca.pem` on `/data` (5.10); the runbook checks from the board |
| docs/07's `push-update.sh` example matched several bundles, from the wrong directory | the deploy directory's link, from the repository root; `push-update.sh` refuses a file as the board |
| docs/06 pointed at the Phase 0 runbook for a UEFI-menu recovery that the runbook says is yet to be found | a USB reflash, which empties `/data`, is given as the only recovery known; test 28 stays open until the menu path is found and written down |
| the bench build blocks ran on after a failed build - copying the previous bundle or flashing an older image - the fault bundles had no release, and the `--stream` soak reused versions the board had already run | `set -e` in every block, a release for every bundle, and a second, higher set for `--stream` |
| docs/07's first A/B flash skipped `04-unpack-tegraflash.sh`, so it could write an older single-slot unpack back | the step is there, and `05-flash-nvme.sh` refuses an unpack whose layout does not match `AB_LAYOUT` |

### Review pass 12 (internal multi-agent review of the implementation, after pass 11's fixes)

A second review of the whole implementation by independent agents, after
pass 11's fixes, each finding checked by a second one against the code and
the upstream sources; then a review of those fixes before the pull requests,
and CodeRabbit's. The boot and fallback findings:

| finding | resolution |
|---|---|
| a slot that failed to load was marked bad only in memory when `grubenv` was unreadable or the ESP refused writes, so every reset picked it again until NVIDIA's sticky recovery, while the other committed slot was never tried | when the mark cannot be saved, GRUB hands over within the same boot, uncounted, to a committed slot (`boat.handover=1`) or to the last resort without the failed slot, and `boat-grubenv-guard` writes the mark from Linux and alerts (5.3) |
| the guard's rebuild of a torn `grubenv` on a last-resort boot made the booted slot committed, though GRUB passes the same flags whether or not it ever passed a trial: a release that failed its trial became the committed OS unchecked | on a last-resort boot the rebuilt slot is on trial from the next boot (`COMMITTED=0`), the other unusable (5.3) |
| RAUC's `grub-editenv` reported a failure when only the `grubenv.bak` copy failed, after `grubenv` had already changed: an install was reported failed although the new slot was active | the wrapper fails RAUC's call only when `grubenv` was not written; a failed `.bak` copy is an alert, which the guard mends on the next boot (5.3) |
| a commit whose `grubenv.bak` copy failed was reported as a failed write of `grubenv` | it is still not a commit - GRUB falls back to that copy after a torn `grubenv`, and would take the slot for a trial again - but its record names the copy (5.4) |
| outside GRUB with `L4TDefaultBootMode` already 0, the board said only that it had booted slot A, and the docs said "reboot once" - but then GRUB itself failed to load, every reboot returns to slot A, and A may be the older or a rejected release | `boat-bootmode-guard` alerts that GRUB did not load and a reflash is needed; docs/06 and docs/07 tell the two causes apart. A slot-aware DIRECT path and `panic=10` there are left out: the first boots a slot with A's kernel, the second only reaches NVIDIA's sticky state sooner (5.1, 5.5) |
| changes to `grub.cfg`, GRUB or `KERNEL_ARGS` never reach a board updated over the air, and nothing said so or noticed it | docs/07 says they need a USB reflash. `grub.cfg` passes `boat.esp=N`; the guard alerts only when the running release needs newer boot files than the ESP has (`BOAT_ESP_MIN`), and an ESP merely older than the newest is a note in its journal, since a reflash empties `/data`. Refusing such a bundle before install is left for later (5.9) |
| `boat-commit` has no default dependencies, so it still ran in emergency mode and reset a committed slot's attempt count: a slot that kept ending in emergency mode was rebooted for ever, never handed over | `boat-commit.service` conflicts with emergency and rescue mode, so the count stands and GRUB falls back (5.5; test 15) |
| the emergency reboot also ends an operator's own `systemctl rescue` after 60 s, and no user document said so | docs/07 says so, and how to keep the shell (debug-hold, or stopping the unit); the unit's comment too (5.5) |
| `BOAT_AB_LAYOUT=1` without `UBOOT_EXTLINUX=1` built and flashed an image with no `/boot/initrd` in either slot | `boat-ab-boot` fails the parse (5.1); a harness case runs the check |
| `test-grub-ab.py` only passed with the host's GRUB 2.12 `grub-emu`: 2.06, the board's, sets `$cmdpath` and has no emu `linux` module | both `$cmdpath` messages are accepted, `linux`/`initrd` map to the `linux` module, and a GRUB build tree's own lists are used; it passes under 2.06 and 2.12 (7) |
| the GRUB harness never booted slot A on trial, which every second update does: booting A's trial as committed, or uncounted, passed every scenario | A's trial, its read-only ESP and torn-`grubenv` pass-overs and its failed load are scenarios (7) |

The trial, sleep and storage findings:

| finding | resolution |
|---|---|
| the A/B lock file was 0644, and `flock` locks a file opened read-only, so any local user could hold every `grubenv` writer off - failing every trial and blacklisting each release | the lock files are 0600, made by a tmpfiles.d entry at boot (`boat-update`'s `/run/boat-update.lock` too) and kept so by `lib.sh` (5.3) |
| a trial failed for a reason the board can cause too (the ESP, `grubenv`, RAUC, a commit the ESP refused) blacklisted the release with nothing to tell it apart | it still fails - `boat-commit` cannot tell a board fault from a release that caused it - but its record says so and how to install the release again by file, and docs/07 says how (5.4) |
| `boat-update rollback` off an undecided trial was taken for a skipper's shutdown: the rejected release got its attempt back behind the slot rolled back to, and was never recorded as failed | `--shutdown` treats a reboot to the other, usable slot as a verdict: the slot is marked bad and the release recorded as failed (5.4) |
| that shutdown record could be written after `/data` was unmounted, and was then lost | `boat-trial-inhibit` is ordered after `data.mount`, so it stops before `/data` is unmounted (5.4) |
| the gate's last round, with the operator's `health.d` checks, could run past `boat-commit.service`'s 10-minute limit and be killed before its verdict, failing a good release at the deadline | each check gets only the time left before that limit, and `BOAT_HEALTH_CHECK_TIMEOUT` is validated (5.4) |
| that limit left out `timeout`'s kill grace, and the commit could wait for the A/B lock past the unit's limit | the checks stop 10 s before the limit, the kill grace counted, and the commit waits for the lock only as long as the unit still allows (5.4) |
| `boat-commit`'s verdicts went only to `last-result`, which the next one overwrote, while the docs sent operators to `boat-update history` for them | every verdict is appended to `history` as well (5.4) |
| the trial deadline and the shutdown retry sourced `health.conf` like the gate, so one unreadable line there left a trial that never ended (and failed the release at the next power cut) | `--deadline`, `--shutdown` and `--may-sleep` never read it, the gate tries it in a subshell first and alerts, and the deadline's unit reboots if its own run fails (5.4) |
| that subshell test took a `health.conf` whose last line is a false test (a `[ -x ... ] && ...`) for an unreadable one, and dropped the operator's settings | the file is read unless `sh -n` finds a syntax error or it stops before its end (5.4) |
| `rauc.service`'s `RequiresMountsFor=/data` restarted DATA's failed check on every on-demand start - every `boat-update status` | `Requisite=`/`After=data.mount`: it waits for a mount under way and fails at once otherwise (5.8) |
| `boat-install-firefox` wrote to the slot, so every update dropped the helm's Firefox, and a rollback met a profile a newer Firefox had written | on the A/B layout it installs to `/data/opt/firefox`, and a boot unit puts the menu entry back into each new slot; docs say anything else installed in a slot is lost (5.8) |
| its swap into `/data` copied from the slot's `/var/tmp`, so a full `/data` could leave a half-copied Firefox | the working copy sits beside the install, the swap is a rename, and a failed swap puts the previous Firefox back |
| the link unit's `ConditionPathIsExecutable=` is a key systemd does not know, so the unit ran unconditionally | `ConditionFileIsExecutable=`, and a wiring check fails on any `Condition`/`Assert` key systemd v250 does not know |
| `boat-install-firefox --remove` without `/data` removed only the slot's launcher, which the link unit put back, and reported success | it refuses while `/data` is not mounted |
| the gate does not see the helm display or the compose stack, and `health.d` was undocumented, silent and could hang on a check that ignored SIGTERM; a `/data` unit could not be required safely | the display stays out of the gate by design, now said (5.4); `BOAT_HEALTH_DATA_UNITS` for units on `/data`, `health.d` documented, with `timeout -k` and its output in the journal |
| the gate passed a trial while DATA was still being checked - RAUC, Docker and `health.d` were skipped as if DATA had failed | a trial is not decided while DATA's check or mount is under way; at the deadline it is decided without them (5.4) |
| nothing at the helm shows a trial or an ALERT, and docs/07 never said the power key does nothing while an install writes | docs/07 says so; a helm-side indication stays out of scope, recorded as a limit (5.4) |
| `/etc/boat-ab-nocommit` switched the gate off undocumented, in every image, and the deadline still failed such a trial | the switch is gone; a harness case keeps the gate free of switches the deadline lacks |
| root's own `systemctl suspend` went through the trial and install inhibitors (logind never holds root back with root's locks), and the gate's retry clock counted time asleep | a drop-in runs `boat-commit --may-sleep` before every suspend; time asleep is given back to the checks (5.4, 5.6; test 27) |
| `boat-sleep --status` and `--dry-run` never ran the trial, install or lock refusals they are the diagnostic for | both run the same checks as a real run; `--dry-run` exits 1 where it would refuse (5.6) |
| `boat-sleep` knew a trial only by `boat-trial-inhibit` running, so early in a trial boot, or with that unit failed, it slept | it reads `boat.trial=1` and this boot's commit record, as `boat-update` does (5.6) |
| a connection flood to port 22 tripped `sshd.socket`'s trigger limit, failing it for the boot - and with it every trial | `sshd.socket` has no trigger limit (a drop-in), and the gate restarts a socket stopped by one (5.4) |
| no harness looked at the wiring the shutdown retry and RAUC's `grub-editenv` depend on | `test-ab-scripts` checks the units, the drop-ins and the recipes' install paths against each other |
| the RAUC handler cases only ever installed into slot B | cases install into, refuse, and activate slot A from slot B |
| DATA's check, with no time limit, held the helm desktop and non-root logins off, and nothing said why | kept, deliberately (no login into a stale home), and now said: on the console, in the units and in docs/07 (5.8) |
| the DATA seed's journal was a hole, so a flash that skips holes (`bmaptool`) left the previous filesystem's journal, same UUID, for a crash to replay | the recipe writes the journal out as zeros; a harness case builds the seed and checks it (5.2) |
| journald's default `SystemKeepFree` (up to 4 GiB) on a nearly full DATA stopped the journal growing, losing older boots sooner | `SystemKeepFree=512M`, well below `boat-update`'s free-space floor (5.8) |
| the storage harness stubbed every command and never looked at the order systemd starts DATA's units in, so a lost `After=`/`Before=`/`Requires=` passed | `test-boat-storage.py` runs `boat-storage.bb`'s `do_install` and checks each line of the DATA, swap, grow and RAUC wiring 5.8 relies on, the start order end to end, that every DATA mount is `nofail` and nothing early waits for DATA; deleting any of those lines fails it |

The update client findings:

| finding | resolution |
|---|---|
| the clock gate asks `chronyc`, which is a package of its own that nothing installed, so on the board `timedatectl` always decided, untested | `boat-update` recommends `chronyc`; the harness also runs the gate with only `timedatectl` (5.10) |
| `systemd-run` and `systemd-inhibit`, which every install and the clock retry run, reached the image only as a recommendation of systemd's | `boat-update` depends on `systemd-extra-utils` |
| the link was the main table's default route, and transfers were bound to it: a channel behind a split-tunnel VPN could not be reached, a policy-routed WireGuard default was bypassed, and an IPv6-only link was "no network" | the link is the device the kernel routes the channel's address through (`ip route get`), with the metered and VPN rules applied to it (5.10) |
| this image's NetworkManager has no ModemManager plugin, so a USB modem or a tethered phone was guessed unmetered and downloaded over cellular | a device on a USB modem or tethering driver counts as metered unless its connection says `metered no`; docs/07 and test 36 say so (5.10) |
| on a fleet moving to a new L4T, every later release published without `--reflash` was an alert on each boat not yet reflashed | a newer release on another L4T is "reflash at the dock" with or without the flag, never an alert; docs/07 says how to publish during the move (5.10) |
| `boat-update` never read the index's `firmware`, so a release on the same L4T built against another device tree was offered and downloaded to READY, then refused by the pre-install handler; the docs said such boards were told to reflash | the index's `firmware` is checked against the board's `/usr/lib/boat-ab/firmware.id` before any download: a mismatch is "reflash at the dock", not an alert, as for a BSP bump; a side without the record is offered as before (5.9, 5.10) |
| any local process could fill the stream relay with idle connections, each holding a thread and a file, and make a streamed install fail | a connection from anyone but RAUC is closed as it is accepted, a refused request closes its connection, an idle one is closed after a minute, and only so many are held (5.10) |
| the relay needed a self-signed root in `server-ca.pem` where curl took an intermediate, so streams failed where checks worked | the relay trusts what curl trusts; docs/07 and `channel.conf` ask for the root CA (5.10) |
| a stop during the mount of the other slot, a closed terminal, or a busy mount point could leave that slot mounted, and RAUC then refused every install into it; the probe mount was also open to every local user (no `nosuid`) | the flashed slot's release is read with `debugfs`, without mounting it; a hang-up unwinds like a stop (5.10) |
| a typo in `/data/boat-update/channel.conf` stopped `rollback`, `status`, `history` and `debug-hold` as well as every check | only the verbs that use it read it; `status` shows the typo as an alert, and the timer's run fails (5.10) |
| `boat-update install <file>` read `channel.conf` first, so a typo or a refused URL in it stopped `push-update.sh`'s offline install | an install that does not stream goes on with the defaults and a warning when `channel.conf` cannot be read (5.10) |
| `BOAT_UPDATE_CHANNEL` defaulted to `stable` in `BOAT_BENCH` images, which then refused every stable index as an alert | the recipe defaults it to `bench` when `BOAT_BENCH = "1"` (5.10) |
| a redirected or missing bundle was a window without progress, repeated for ever without an alert, and a redirect's page stayed at the start of the `.part` | a redirect or any answer but the bundle is an alert, and its bytes are cut off the `.part`; an index that redirects is reported as a redirect (5.10) |
| a check that got no answer still used up the 30-minute rate limit, so the link-up trigger that would have worked did nothing | only a check the channel answered stamps it (5.10) |
| any pending offer lifted the rate limit, so with `AUTO=check` or a ready bundle every network event fetched the index again | within the window nothing asks the channel again; only an unfinished download resumes (5.10, test 49) |
| a background download never re-read `channel.conf`, and a stale "waiting: metered link" stayed in `status` while it downloaded | a background download follows `channel.conf` as it is at each window, and the wait is cleared on disk (5.10) |
| a CDN or proxy caching `index.p7` raised replay alerts and delayed withdrawals, and a parallel object-storage sync could put the index up first | docs/07 says not to cache the index and how to upload to object storage; the replay alert names a stale cache as a cause |
| no board-side case had a ready download when the channel withdrew it, lost or replayed its index, rebuilt the version or moved past a withdrawal the board missed; a rebuild under the same bundle name kept the old file "ready" and was never fetched | `test-boat-update.py` runs each case on a READY bundle; a new offer of another file now drops the old download and its ready record, and `install` warns when the last check failed (only a signed index withdraws, 5.10) |
| an offer housekeeping dropped left its partial download on `/data`, unseen | the partial download goes with the offer |
| an `OSError` (a full `/data`, a file gone mid-`status`) ended in a traceback, with no alert | it is a failure with its reason, kept as an alert where it can be; `status` shows a nearly full `/data` itself |
| a trial outcome `boat-update` recorded was not on disk before `state.json` dropped the install, so a second power cut could offer the failed release again | the failed version (and the history line) is fsynced first |
| the bundle hook's refusals and warnings never reached the operator: only lines starting `# boat-rauc-` were shown | the hook's lines are shown too, and its warnings from an install that worked stay in `status` until the trial ends |
| the install wrapper read its result from the unit name every install shares, so a second install's reset-failed could turn a failed install into success | the install's own run reports its exit status to a file only its wrapper knows (5.10) |
| the install wrapper first replayed the previous install's last log lines | it follows the unit's log from the moment it started it |
| a link that dropped mid-handshake or mid-transfer was an alert, and a captive portal's certificate looked like a trust problem only | a connection cut off before any HTTP answer is the board's state; an untrusted certificate stays an alert that names a captive portal as a cause |
| a `BOAT_UPDATE_URL` with a user name and password worked for checks, and put the password in `ps`, `state.json` and `status` | it is refused, without repeating it (5.10) |
| the client-key check missed a key kept inside the client certificate file | a certificate file that holds its key is held to the key's rule |
| a USB reflash emptied the channel settings, and the image had no way to carry the channel URL | `BOAT_UPDATE_URL` and `BOAT_UPDATE_CHANNEL` can be built into the image; docs/07 and docs/06 say to restore the settings and credentials after a reflash (5.10) |
| rows 34, 35 and 45 were said to be covered off-target, but nothing tested the system trust store, an expired index certificate, another `compatible` or another L4T | `test-boat-update.py` does now; the runbook's coverage table lists every harness (section 7) |
| the `nmcli` stub ignored its arguments, so the VPN branch of the metered check never ran | the stub answers per device; a VPN over a metered link and over an unmetered one are tested |
| the harness did not cover HTTP errors, the rate limit with an offer, trial outcomes through `status` and `history`, `download`'s exit 2 on a metered wait, rollback from a trial, a stop's unwinding or an `OSError` | all added to `test-boat-update.py` |
| the harness's 416 had no `Content-Length`, so each run stalled two minutes | it sends one; the run takes about half a minute, and `lint.sh` shows what failed under 3.10 |

The RAUC, bundle and host tooling findings:

| finding | resolution |
|---|---|
| an install with `/data` full or out of inodes lost the fallback slot for nothing: RAUC marks the target `OK=0`, then fails to save its status on `/data` | the `pre-install` handler refuses an install with less than 8 MiB or 64 inodes free on `/data`, before anything is marked; matrix row 38 (5.6) |
| the Docker-major warning reached nobody on a successful install: RAUC shows a handler's output only in its own journal | it is an alert in `status` until the reboot and goes into the install's `history` line; it still does not refuse, `rollback` does not check it, and the risk table names the journal format too (5.8) |
| only the L4T string guarded flash-only firmware: a bundle built against another kernel DTB within one L4T release installed silently | each image records its DTB and overlays as `firmware.id`, the bundle carries it, `pre-install` refuses another value and `publish-update.sh` wants `--reflash`; UEFI is not covered (5.9) |
| an edited `health.conf` was carried forever, so a release that renamed a gated unit failed its trial on every board that extended the gate | a carried unit list naming a unit the new slot lacks gets a merged list appended (the board's units it has, then the release's), and the release's own file goes beside it as `.boat-default` (5.8) |
| a default carried from an image without the record counted as edited at every later update, not "once" as documented | documented as such; the release's version is always beside it as `.boat-default` to take back, and the unit-list merge covers the case that fails trials (5.8) |
| settings changed after an install were missing after a rollback or fallback, and the next install destroyed the newer copy for good | the `pre-install` handler keeps the connections, pairings and `authorized_keys` the target holds and this slot lacks or has otherwise in `/data/boat-update/displaced`, with an alert; docs/07 and matrix row 29 say so (5.8) |
| that keeping raised a false alert on every ordinary forward update and copied the older slot's stale PSKs, revoked keys and BlueZ-rewritten pairings to `/data` | only files changed after the later of the two slots' installs (RAUC's `installed.timestamp` in its central status file) count, nothing without either record, and the target is mounted `ro,noload,nosuid,nodev,noexec`; the clock dependence is a stated limit (5.8) |
| `conffile-defaults.sha256` was recorded before `sort_passwd`, `rootfs_reproducible`, the package-data removal and `ldconfig` changed `/etc` | it is recorded first in `do_image`, after everything `do_rootfs` does (5.8) |
| the plan said the bundle key could live on a PKCS#11 token, but `boat-bundle` overrode `RAUC_KEY_FILE` and required a file | a `pkcs11:` URI in `local.conf` works, with `RAUC_PKCS11_MODULE` and `libp11-native`; a token that needs a PIN is not supported (5.7) |
| the image keyring was installed from `BOAT_KEYS_DIR` unchecked, so a hand-built rotation keyring could ship the CA's private key on every board | the build refuses a keyring with a private key, a non-certificate block or a non-CA certificate, and compares the bench CA by certificate; `publish-update.sh` refuses a private key too (5.7) |
| `03-build.sh` silently ignored a release, floor or key directory set after `02-configure-build.sh`, so a raised security floor never reached `system.conf` | `03-build.sh` stops when one set in the shell or `env.sh` is not the one in `local.conf`, and logs the release and floor it builds; docs/07 and `env.sh` say to run 02 again (5.7) |
| that check compared `BOAT_KEYS_DIR`, which `env.sh` always defaults, so a build directory configured with its own keys died from every new terminal | the key directory is compared only when the shell gave it; the release and floor, which `env.sh` leaves empty, as before (5.7) |
| `05-flash-nvme.sh` compared the unpack with the shell's `AB_LAYOUT`, so in a new terminal a stale single-slot unpack was flashed over an A/B board | the configured build directory decides, and a contradicting `AB_LAYOUT` is refused; the variable decides only without one |
| `publish-update.sh` needed OpenSSL 3 and rsync 3.2.3 on both ends, and an old OpenSSL showed up as a bundle that does not verify | it checks for OpenSSL 3 first and shows OpenSSL's whole error; the channel directory is made without `--mkpath`; docs/02 and docs/07 name the requirement (5.10) |
| the index (and, locally, the bundle and directories) was published with the publisher's umask, so under 077 the web server could not read it | published files are 0644 and directories 0755 whatever the umask (`--perms --chmod` over rsync) (5.10) |
| the index serial had no lock and depended on the clock, so a second publisher with a lagging clock published a serial boards reject as a replay | publishes take a lock next to the counter, and the serial is also above the channel's current index, read back from the destination (5.10) |
| `push-update.sh` passed ssh-style `BOAT_SSH_OPTS` to scp, so the documented `-p PORT` broke the no-internet path | it copies over ssh, as `ab-soak.sh` does (5.10, Phase 5) |
| `push-update.sh` failed for `BOAT_SSH_USER=boat`, which `wol/README.md` documents | a non-root user runs the board's commands through `sudo -n`, as `boat-sleep.sh` does |
| `push-update.sh` left multi-GB bundles on `/data` after a failed or `--no-reboot` install, and its sessions had no keepalive | its own copy is removed on every exit, with copies earlier runs left a day ago or more, and ssh keeps the session alive |
| that cleanup removed every bundle in the incoming directory, including one another push was copying or installing (CodeRabbit) | only this push's bundle and `.part`, and leftovers untouched for a day |
| `publish-update.sh` went on as if the channel were new when it could not read the channel's index back, with no serial or device tree to keep above (CodeRabbit) | only a missing index - a channel's first publish, which rsync reports with exit 23 and a `link_stat` error naming that `index.p7` - goes on; any other failure, including a "No such file" about something else, stops the publish with rsync's exit status and message |
| `publish-update.sh` treated a channel index that it read back but that did not verify under the key directory's CA, or had no readable serial, like no index at all, so the serial and device-tree checks were skipped (CodeRabbit) | such an index stops the publish with openssl's message; only a missing index goes on |
| an unset `RAUC_KEY_FILE` failed `boat-bundle`'s parse in its checksum expression, before the recipe's own check could say what was missing (CodeRabbit) | an unset key adds nothing there, and `boat_check_bundle_keys` reports it |
| two `test-boat-update.py` checks passed only as root and without chrony on the host, and failed on CI's runner | a `chronyc` that fails stands in for none, and the client key's owner check goes by the real uid while the harness poses as root |
| `push-update.sh` and `ab-soak.sh` judged the reboot by ping, which a jump host or an ssh_config alias does not reach | both wait for a new boot ID over SSH (`wait_for_new_boot`) |
| the TypeScript client's `listen()` bound 9099 beside `boat-sleep-listener` with `SO_REUSEADDR` and silently took every sleep request on the boat | it no longer sets `reuseAddr`, so a taken port fails with `EADDRINUSE`; the README says it belongs on another host or port, and that `onSleep` on the boat must hand off to `boat-sleep` |
| the layout harness never ran NVIDIA's real template order (ids, `APP`/`APP_b` last), and the initramfs harness ran `/init` under one shell | `test-ab-layout.py` runs NVIDIA's R35.6.4 partition list too, and `test-ab-initramfs.py` every POSIX shell present (busybox ash when installed); the storage units' ordering is left to `test-boat-storage.py` |
| the bundle-hook harness never checked the machine-id, sleep key, time zone, boot control, listener drop-in or pairing carry-over | each is now asserted byte for byte (and mode, link) in the new slot |
| the publish harness never checked the index's `sha256` and `size` | both are asserted, and the published index passes `boat-update`'s own `validate_index` and `decide` |
| no harness modelled fleet flows: promotion, withdrawal, version reuse, a stale index, a second publisher | `test-publish-update.py` runs them through `boat-update`'s own checks; the board-side cases with a ready download pending are in `test-boat-update.py` |

The documentation findings:

| finding | resolution |
|---|---|
| docs/06's fix for a reboot during every sleep, `BOAT_SLEEP_STOP_WATCHDOG=1`, does nothing on an A/B image, where systemd holds the watchdog | docs/06 and docs/05 say so, and send such a reboot to the bench (test 30); a sleep-time switch for systemd's watchdog is left out, since it would turn off what the fallback relies on (5.5) |
| docs/06 called on-board edits of the helm's keyboard and screen-blank settings permanent, though they live in the slot, and three places blamed sleep locks on an XFCE power manager the image does not install | docs/06 points at the build (`BOAT_HMI_XKB_LAYOUT`, meta-boat's `startx` arguments) or an autostart entry in `~boat` on `/data`; docs/05, docs/06 and `boat-sleep` name the locks the image holds |
| the dock-reflash recovery flashed whatever `boat-image` the build directory built last - a bench image after a bench run, or an older release - and left out the build and `AB_LAYOUT=1` | docs/07 "Reflashing at the dock" gives the whole sequence with the release named, and the sticky-state and BSP-bump entries in docs/06 and docs/07 point at it; a check in `05-flash-nvme.sh` that tells such an unpack apart would have to read the unpacked rootfs, and is left for later |
| docs/07 and 5.10 said bundles hold nothing secret, but every bundle holds the image's `/etc/shadow` - also a private `BOAT_PASSWORD_HASH`, which the docs tell operators to set | docs/07, docs/05, the README and `boat-image.bb` say a private hash needs a channel with a client certificate, or locked passwords on the boards (5.7, 5.10) |
| how to withdraw a bad release was undocumented; the obvious way, deleting `index.p7`, withdraws nothing; and "the worst a channel host can do is withhold updates" left out a withdrawal it withholds | docs/07 and 5.10 give the procedure (publish the release before it again) and what it does to a board in each state; docs/07 and 5.7 say a board that missed the withdrawal can still be served the older index. A withdrawal field or an index expiry is left out (5.7, 5.10) |
| `boat-update` status, documented without `sudo`, cannot ask RAUC (its D-Bus is root's alone), so it showed the other slot's release as "unknown" with no reason and missed a half-written slot | the docs say `sudo boat-update`, and without root `status` says why (Phase 5) |
| a good release that failed its trial only through power cuts was never offered again, and nothing said how to try it again | docs/07 and docs/06: install the bundle by file, which does not consult the failed list, or delete its line from `failed-versions` and check again; the list still never shrinks by itself |
| only root's `.ssh` is carried, so a `docker login` as root (which `boat-compose` pulls with), a `.gitconfig` and scripts in `/home/root` were lost at each install, while docs/07 read as if root's home were kept | docs/07, docs/05 and 5.8 say what is carried and to log in to a registry again after an update (5.8) |
| a rollback or a GRUB fallback silently brings back credentials revoked since the other slot was installed: a password, a root SSH key, the sleep key | `boat-update rollback` warns before it reboots, and docs/07, 5.8 and test 29 say to revoke them again; a fallback stays silent, and keeping them on `/data` would lose root's SSH when DATA breaks (5.8; test 29) |
| the UEFI watchdog was said to cover every GRUB failure, but it is a firmware timer event that cannot fire once GRUB crashes into the firmware's exception handler; the patch also said the kernel disarms it | 5.1, 5.5 (a row of its own), section 9, docs/07 and runbook 2.3 say a crash needs the Tegra WDT or a power cycle, after which an attempt GRUB had counted falls back, and one before the count was saved needs a reflash (CodeRabbit); the patch says the firmware's `ExitBootServices` disarms it (5.1, 5.5) |
| plan statements the code does not match: Tier 1's `rauc status` on every boot, a read-only `status`, a clock retry after two minutes, a stall threshold kept below the rate limit | 5.4, 5.10 and Phase 5 say what the code does: `rauc status` only with DATA, root's `status` records a trial outcome, 2 to 32 minutes, a fixed 1 KiB/s (5.4, 5.10) |
| section 8's migration backed up a `/root/.ssh` that does not exist, restored slot files its own round trip then dropped, gave no Docker copy flags, and asked for an over-the-air round trip one release cannot make | it backs up `/home`, restores only what survives an update, gives the Docker flags, and proves the round trip by file or with two newer releases on a channel of the board's own (8; test 29) |
| nothing steps a long-offline board through releases or records which release last wrote `/data`, and what every bundle must keep for the oldest release in the field was written down nowhere | 5.10 "Forward compatibility" and docs/07 list those interfaces and the stepping-stone or reflash procedure; a `min_from` index field and a migration framework are left out - a release already in the field would ignore the field (5.10) |
| the bench runbook's row 5 could not be built (a floor above the image's own release), and its key step could mint a second fleet CA on a separate bench host | row 5 builds release 2.0.0 with the floor; the key step copies only the fleet CA certificate to a bench host |

### Your review

Every decision in section 10 was taken at its recommendation (D1-D15) when
the plan was merged, and implementation started with Phase 0.

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
  - meta-rauc `rauc-mark-good.service`;
  - `docs/advanced.rst` "HTTP Streaming" (needs verity bundles, Range
    requests, kernel NBD) and "Adaptive Updates" (`block-hash-index`, ~10%
    of a bundle for a small ext4 change, indices kept in the data directory);
  - `docs/reference.rst` `[streaming]` (`tls-cert`, `tls-key`, `tls-ca`,
    `send-headers`);
  - `src/main.c` (`rauc install` takes `--tls-cert`, `--tls-key`,
    `--tls-ca`, `--http-header`);
  - `src/install.c` ("Leaving target slot non-bootable as requested by
    activate_installed == false").
- **GRUB 2.06:** `grub-core/kern/efi/init.c` (disables the UEFI watchdog);
  `normal/menu.c` (falls back to a prompt); `util/grub-editenv.c`
  (truncate + write).
- **Prior art:** meta-mender-community
  `meta-mender-tegra/meta-mender-tegra-jetpack5` (capsule-driven switch,
  `nvbootctrl verify`, gated `nv_update_verifier`). It supports AGX Orin,
  AGX Xavier and Orin Nano, all of which have eMMC for chain B.
  meta-rauc-community `meta-rauc-tegra` covers U-Boot/R32 boards only, so it
  does not apply.
