# Plan: A/B OS updates with automatic fallback

**Status: v4, being implemented phase by phase (section 6).** Every
decision in section 10 was taken at its recommendation. Phases 0 to 6 are
in the repository, behind `AB_LAYOUT` (off by default); their exit tests
need the bench board. Section 11 records what each review pass found and how
the plan answers it.

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
  falls back when the *load* fails, so `grub.cfg` must never stop at a
  prompt (5.3).
- **GRUB hangs: keep the UEFI watchdog armed.**
  - L4TLauncher arms a 5-minute UEFI watchdog before starting GRUB, but
    stock GRUB 2.06 cancels it in its EFI init
    (`grub-core/kern/efi/init.c`: `set_watchdog_timer(0, …)`).
  - A one-line `meta-boat` patch to `grub-efi` **re-arms it instead**
    (300 s).
  - A GRUB that wedges, or a kernel load that never returns, then ends in a
    firmware reset.
  - The UEFI spec has `ExitBootServices` disarm the watchdog, so it stops
    the moment the kernel takes over. There is no menu (`timeout=0`), so it
    can never fire while someone is reading the screen.
  - Phase 1 proves it with a `grub.cfg` that only sleeps. The Tegra hardware
    WDT, if Phase 0 shows it runs this early, is a second layer, not the
    one the design depends on.

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
| 18 | `DATA` | min 2 GiB, **fill-to-end** (`0x808`) | small ext4 seed image via `DATAFILE` (Phase 2) | online `resize2fs` on first boot |

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
| `A_TRY` / `B_TRY` | GRUB counts up, `boat-commit` resets to 0 | **unconfirmed boot attempts** (a count, not a flag) |
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
| committed A, one power cut mid-boot | A(TRY 1) → cold boot → A(TRY 2) → `boat-commit` resets TRY | stays on A (cold boot also re-seeds NVIDIA's register; Phase 0 confirms this) |
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
  - the kernel booted the slot named by `rauc.slot`, from the partition
    that slot is configured for, and its GPT name matches;
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

- Leave meta-rauc's `rauc-mark-good` package out of the image (the `rauc`
  recipe only recommends it). One unit does the gating and the marking:
  `boat-commit.service`. It is `Type=oneshot` and
  `DefaultDependencies=no` (so no target orders after it), `After=` the
  critical units, `WantedBy=multi-user.target`, and retries checks for up to
  `TimeoutStartSec=10min`. It is *not* ordered after Docker, which waits for
  `/data` and its check: the checks retry for Docker instead, so nothing on
  `/data` can hold up the verdict.
- On success: `<slot>_OK=1 TRY=0 COMMITTED=1` in **one atomic `grubenv`
  write** (copy, `grub-editenv`, rename), read back. Not `rauc status
  mark-good`: RAUC's grub backend rewrites `grubenv` in place, and a power
  cut that tears that write sends GRUB to `grubenv.bak`, where the passed
  trial still looks used up.
- **Trial failure:** `<slot>_OK=0` the same way, record the reason in
  `/data/boat-update/last-result` (best effort, and only if `/data` is the
  `DATA` partition), then reboot. GRUB skips the slot.
- **Committed boot:** the attempt counter is reset *first*, before any
  check that may take a while.
- **Committed Tier 1 failure:** no mark, no reboot. An alert shows in
  `status`. The attempt counter is still reset, since the slot did boot, so
  the slot keeps running and the GRUB counter only hands over if it then
  *also* fails to boot, twice.
- `boat-trial-deadline.timer` (`ConditionKernelCommandLine=boat.trial=1`,
  `OnBootSec=15min`): if the trial still isn't committed, mark-bad and
  reboot. This catches a commit unit that hangs. The gate and the deadline
  take one lock, so only one of them decides, and a commit stops the timer.
  The deadline goes by this boot's own record of a commit that fully
  succeeded (`/run/boat-ab/committed`), not by `COMMITTED=1` in `grubenv`:
  a write that failed after its rename can show `COMMITTED=1` that was never
  flushed, or that `grubenv.bak` does not have.
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
| GRUB itself hangs | **the UEFI watchdog, kept armed through GRUB** (300 s; 5.1). The Tegra hardware WDT is a second layer if Phase 0 shows it runs this early. | `grub-efi` bbappend: re-arm instead of cancel. **Phase 1 exit test.** If firmware ignores the re-arm, this row narrows to "power cycle to recover" and goal 4 says so. |
| kernel panic | `panic=10` | cmdline |
| kernel hard hang | Tegra WDT running from early kernel | Phase 0: armed before userspace? If not, enable it at probe |
| initramfs can't mount root | today: **hangs forever** | `tegra-minimal-init` bbappend: only the two slot partitions, GPT name checked, bounded retries, then `reboot -f` (`boat.stay=1` keeps the shell) |
| systemd emergency/rescue | today: waits forever | drop-ins: reboot after 60 s unless `boat.stay=1`. Shared mounts are `nofail`, so only slot-local faults get here. |
| PID 1 wedged | `RuntimeWatchdogSec=60s`, `RebootWatchdogSec=10min` | **decision D8**: one `/dev/watchdog` opener |
| commit/health hangs on a trial | `boat-trial-deadline.timer` | add |
| power cut during a trial | TRY already counted → next boot skips the trial slot | none (conservative) |

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
- It takes the sleep + shutdown inhibitor until commit (5.4).
- `boat-sleep --force` also checks for an update in progress.

The order matches RAUC 1.13's `install.c`:

| step | who | if power is lost here |
|---|---|---|
| 1. verify signature and verity format | RAUC | nothing changed |
| 2. system `pre-install` handler: BSP/L4T and Docker-version checks from the bundle's `[meta.boat]` manifest section; set target `COMMITTED=0` (and nothing else: step 3 can still refuse the bundle, and the target must then stay a usable fallback) | ours | target merely marked not-committed |
| 3. check `compatible` and `min-bundle-version` (5.7) | RAUC | target not-committed; a refused bundle leaves it bootable, as a trial |
| 4. set target `OK=0` | RAUC grub backend | target unbootable; stay on current |
| 5. write image into the target slot, `resize=true` | RAUC (`type=ext4`) | target half-written, `OK=0` |
| 6. slot `post-install` hook: carry-over into the new slot (5.8) | ours | target still `OK=0` |
| 7. activate: `ORDER=target first`, `OK=1`, `TRY=0` | RAUC | new slot boots **as a trial** (COMMITTED=0 since step 2). If `grubenv` is torn, see 5.3 integrity. |
| 8. system `post-install` handler: append history on `/data` | ours, best effort | none |
| 9. reboot (operator or `--reboot`) → trial → commit or fallback | 5.3 / 5.4 | TRY counted → fallback |

- **No post-write read-back hash.** RAUC resizes the filesystem before slot
  hooks run, so a hash taken in the hook would never match.
- **Manual rollback:** `boat-update rollback` runs
  `rauc status mark-active other` and reboots. It refuses if the other slot
  is `OK=0`. It is a slot switch; nothing is reinstalled.
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
  live on a PKCS#11 token.
- **Compatibility and downgrade:** `compatible=boat-xavier-nx` rejects
  bundles for other boards. RAUC's `min-bundle-version` (D6) covers
  downgrades. Falling back after a failed update is a slot switch, not an
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
    time-stamped authority; accepted.
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
    `/etc/default/boat-bluetooth`, `/etc/docker/daemon.json`,
    `/etc/boat-update/health.conf`);
  - the password hash fields of `root` and `boat`, merged (**D5**);
  - `/etc/nv_boot_control.conf`.
  - Not on this list: the OTA channel configuration. Its defaults ship in
    the image (`/etc/boat-update/channel.conf`), and the operator's
    overrides and client certificate live on `/data` (5.10), so both slots
    always see the current values.
- **Lives on `/data`:**
  - Docker and compose (already there);
  - RAUC's `data-directory` (`/data/rauc`, with `RequiresMountsFor=/data`
    on `rauc.service`);
  - `boat-update` history;
  - a size-capped persistent journal (`/data/log/journal`), so
    `journalctl -b -1` after a fallback shows why the trial failed;
  - **the login users' homes** (D11): `/home/boat`, bind-mounted from
    `/data/home/boat` (`BOAT_AB_HOME_USERS`). This keeps `~boat/.ssh`, the
    XFCE settings and anything users keep in their home across updates. It
    is seeded from the image's copy on first boot. `root`'s home is
    `/home/root` on this distro and stays in the slot, so key-only SSH as
    `root` still works when `DATA` is what broke; its `.ssh` is carried
    over instead (above).
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
| `serial` | increases with every publish (at least the time in seconds, below 2^32). The board stores the highest serial it has accepted on each channel (on `/data`) and rejects lower ones, which stops a replayed or pulled old index. |
| `compatible`, `version`, `l4t` | checked before any download (below) |
| `bundle` | a bare file name: no `/`, no `..`, no scheme |
| `size`, `sha256` | transport integrity of the download |
| `published` | UTC. Shown in `status`, so a frozen channel is visible. |
| `requires_reflash` | this release is a BSP bump, which cannot ship as a bundle (5.9). Only evaluated for a *newer* version. |
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
  points at a file that isn't there yet. `serial` is the larger of a
  counter kept next to the keys (plus one) and the time in seconds, and is
  recorded before anything is uploaded; an unreadable counter stops the
  publish.
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

**Channels:** `testing` (bench) and `stable` (the boat). To promote a
release, re-publish the same bundle file with a new `stable` index. Bundles
are never renamed, because names are content-addressed.

**Access:** the default is a **public** channel. Bundles carry nothing
secret: the image's password hash is already public (README
"Credentials"). If you want access control, use a **TLS client
certificate** (D15). Basic auth and bearer headers leak in this setup,
because curl and RAUC forward them across redirects and RAUC logs request
headers (review M3).

#### On the board: check, download, ready (stage 1)

`boat-update` gains three verbs, and `status` reports all of their state.
**`check`, `download` and `install` share one `flock`.**

- **`check`:**
  1. **Gates.** It runs only when:
     - chrony reports a synchronised clock (TLS fails at 1970 anyway);
     - the booted slot is committed (no OTA during a trial);
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
     - `version` is not already in the other slot. Only
       `rauc status --detailed --output-format=json` reports it, as the
       slot's `slot_status.bundle.version`; plain `--output-format=json`
       leaves `slot_status` out. A slot that was flashed rather than
       installed has no bundle version, so for slot A as flashed the check
       reads the `/etc/boat-release` inside it (mounted read-only) - the
       same `BOAT_RELEASE` string a bundle carries;
     - `version` is not on the board's list of versions that **failed a
       trial here**.
  5. **Then the L4T check.** `l4t` must match the running
     `/etc/nv_tegra_release`. If it doesn't and `requires_reflash` is set,
     report "BSP bump: reflash at the dock".
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
      file (the server ignores Range) starts again from zero, uncounted.
    - It keeps a `/data` free-space floor (bundle size + 2 GiB, because
      Docker shares `/data`) and re-checks it every window.
    - A bundle is **re-fetched from scratch at most 3 times**, then
      **given up** (by its SHA-256) until the channel publishes a new
      index. A transfer fault is never recorded as a failed trial.
  - **Redirects:** none (`--max-redirs 0`). If the host redirects, the URL
    in `channel.conf` must name the final location.
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
  triggers cost nothing.

**Configuration** (review M6) has two layers, and neither is carried over:

- `/etc/boat-update/channel.conf`, shipped by the image: defaults such as
  the channel URL. An update can change these.
- `/data/boat-update/channel.conf`, the operator's overrides and the
  optional client certificate/key. It is shared by both slots, so a
  credential rotated after an update is still right after a rollback. OTA
  needs `/data` anyway.

| knob | default | meaning |
|---|---|---|
| `BOAT_UPDATE_URL` | image default | channel base URL (final location; no redirects) |
| `BOAT_UPDATE_CHANNEL` | `stable` | which channel to follow |
| `BOAT_UPDATE_AUTO` | `download` | `off` / `check` (notify only) / `download` (also fetch in background). **Never installs or reboots** (D12). |
| `BOAT_UPDATE_METERED` | `no` | download over a metered link? (D14) |
| `BOAT_UPDATE_RATE_LIMIT` | empty | curl `--limit-rate`; the stall threshold (`--speed-limit`) is kept below it |
| client cert/key | none | TLS client authentication (D15) |

**Metered detection** (review m1):

- The board takes the default-route device's own NetworkManager `Metered`
  property and binds curl to that device (`--interface`), so the bytes go
  where the decision was made.
- `yes`, `guess-yes` and `unknown` count as metered. `guess-no` counts as
  unmetered.
- NetworkManager guesses `yes` for ModemManager (cellular) links. **A 4G
  router reached over Ethernet or Wi-Fi reads `guess-no`**, so docs/07
  tells you to mark that connection with
  `nmcli connection modify <c> connection.metered yes`.
- A VPN (WireGuard) default route hides the underlying link. With one
  active, the board takes the conservative answer: metered if **any**
  active device is metered.
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
    that was checked as not metered. Anything but a 206 of the size asked
    for is a 502 to RAUC, which fails the install; a link that goes away
    fails it too. RAUC gets no TLS options, so the key stays root-only and
    nothing credential-bearing is on any command line.
  - The bundle URL is the one from the verified index, on the channel the
    offer was checked on. A preflight request for the first byte refuses a
    redirecting or Range-less channel before RAUC marks the slot
    unbootable, and the installed release is checked against the index
    afterwards (5.7).
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
  - a test `grub.cfg` that only sleeps is reset by the UEFI watchdog within
    about 5 minutes.

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
    (`/var/log/journal`, or `/var/volatile/log/journal` with
    `VOLATILE_LOG_DIR`, since a mount unit must name the real path).
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

- **`boat-update` (on the board).** `status` is the default and read-only.
  It shows:
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
  - **`scripts/push-update.sh <host> [bundle]`**, the no-internet fallback:
    copy to `/data`, install, reboot, then *wait and report which slot came
    back*, in the style of `wol/boat-wake.sh`.
- **A bench update channel:** any local HTTPS server with Range support
  (e.g. Caddy) serving a `testing` channel to the bench board.
- **Exit:**
  - a bundle published to `testing` is downloaded in the background,
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
  - docs/06 troubleshooting, including the sticky-recovery procedure;
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

**Phase 7: validation.** Effort: M, mostly bench time.

- Run the section 7 matrix.
- Soak: 50 update cycles **delivered over the air** from the `testing`
  channel, alternating good and faulty bundles, plus random power cuts if a
  USB relay on the barrel jack is available.
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
  missing `grubenv`, an ESP that refuses writes and a kernel that fails to
  load. (`qemu-system-aarch64` + AAVMF was the first idea; `grub-emu` runs
  the same script without a firmware image, and fits in CI.)
- **Shell logic:** `boat-update`, the handlers, `boat-storage` and the
  boot-state scripts get host-side tests with `rauc`, `grub-editenv`,
  `blkid` and `nmcli` mocked. `scripts/test-ab-scripts.py` runs
  `boat-grubenv-guard` and the commit step as shipped, against a scratch
  directory standing in for the board (`/proc`, an ESP with a real
  `grubenv`, the GPT names). `scripts/test-boat-update.py` runs
  `boat-update` against a local HTTPS channel.
  The OTA `check`/`download` logic also runs against a local Range-capable
  HTTP server, with cut-offs injected mid-transfer and a tampered index.
  `scripts/test-boat-storage.py` runs `boat-storage` against a fake board
  and checks every command it would run on SWAP and DATA.

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
| 24 | a JetPack SD card in the module slot, and a USB stick with `APP`/`esp` labels | ignored: root, ESP and RAUC slots resolve to NVMe partition numbers whose GPT names are checked |
| 25 | unprivileged user, and a container running as a non-root user with the system bus mounted, call RAUC `Install`/`Mark` | denied |
| 26 | `boat-update rollback` after commit | other slot boots and commits |
| 27 | `boat-sleep` (and the UDP sleep listener) during install and during a trial | refused |
| 28 | both slots broken | A, A, B, then UEFI stop (sticky recovery) is the documented end state; the documented recovery works |
| 29 | carry-over | SSH host key, `/root/.ssh`, `/home`, Wi-Fi, BT pairing, hostname, `boat-sleep.key`, passwords (D5) are unchanged after update and after rollback |
| 30 | SC7 + WoL on both slots | works |
| 31 | OTA: channel offers a newer bundle, unmetered link | background download; `status` shows "ready"; no install or reboot happens by itself |
| 32 | OTA: link dropped, SC7 sleep, and reboot, each mid-download | resumes from where it stopped; final SHA-256 matches; never a second full download |
| 33 | OTA: truncated or corrupted download | SHA-256/`rauc info` mismatch → discarded and re-fetched; never offered for install |
| 34 | OTA: index with a bad signature, signed by the bench CA on a boat image, or signed by a public CA certificate | ignored before any download; alert in `status` |
| 35 | OTA: index offers an older version, the same version, the version already in the other slot, a version that failed its trial here, another `compatible`, or another L4T | nothing downloaded; the L4T case reports "reflash needed" only for a newer version |
| 36 | OTA: cellular (metered) link, default policy | check only; download waits for an unmetered link; moving to cellular mid-download pauses at the next window |
| 37 | OTA: clock at 1970 before NTP | check deferred, no TLS-error spam; runs after sync |
| 38 | OTA: `/data` too full, or not mounted | download refused with the reason in `status`; rootfs never used |
| 39 | OTA: client-certificate channel | works for check, download and stream; the certificate is never in the journal or `ps`; still works after an update and after a rollback (it lives on `/data`) |
| 40 | OTA stream: adaptive install of a one-package change | commits; transferred bytes recorded (Phase 6) |
| 41 | OTA stream: link drop mid-install | install aborts; current slot unaffected, target `OK=0`; retry succeeds |
| 42 | OTA stream: server without Range support | clean failure pointing to the download path |
| 43 | OTA: replayed older index (lower `serial`), and a `testing` index served at the `stable` URL | rejected |
| 44 | OTA: a publish caught halfway (bundle uploaded, index not yet) | the board keeps the previous index; no false "bad signature" alarm |
| 45 | OTA: the index-signing certificate past its expiry date | still accepted (`-no_check_time`, as RAUC does for bundles); revocation is by removing the CA |
| 46 | OTA: the server streams an endless body, or more than `size` | aborted at `size`; the `/data` free-space floor is never crossed |
| 47 | OTA: a redirect to another host or to `http` | refused; no credential leaves the board |
| 48 | OTA: two downloads started at once (timer + manual) | the second waits on the lock |
| 49 | OTA: checks across SC7 sleeps and after a link comes up | a check runs on resume and on link-up; at most one per rate-limit window |
| 50 | OTA: check and download during a trial boot | neither runs until the slot is committed |
| 51 | OTA: a 4G router on Ethernet marked metered in NetworkManager | treated as metered |
| 52 | OTA stream after a cold boot, with no manual `modprobe` | works (NBD built in) |

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
4. Put any channel overrides (and the client certificate, if you use one) in
   `/data/boat-update/`. Then, **over
   the air**, install a bundle once (A→B) and once more (B→A) to prove the
   round trip before the boat leaves the dock. After this, the only reasons
   to bring a laptop to the boat are a BSP bump or a dead board.

---

## 9. Risks and open questions

| risk | likelihood / impact | mitigation |
|---|---|---|
| GRUB on this UEFI mishandles the DTB, initrd or cmdline (SC7, display) | M / H | Phase 0 step 2 first; option C is the fallback |
| a GRUB-stage hang with no watchdog running | L / H | GRUB patched to keep the UEFI watchdog armed (5.1); Phase 1 exit test; Tegra WDT as a second layer (Phase 0) |
| Tegra WDT not running in the early kernel | M / M | Phase 0 step 3; enable at probe; otherwise document power-cycle-to-recover for a kernel hang before systemd starts |
| NVIDIA sticky recovery reached (both slots fail, or a shared fault that slipped past `nofail`) | L / H | the 2+1 attempt budget (5.3); `nofail` shared mounts; documented UEFI-menu reset; test 28 |
| `nvbootctrl verify` doesn't reset the count when booted from `APP_b` | M / H | Phase 1 exit criterion; if it fails, `boat-commit` resets it another way, or the budget becomes 1+1 |
| `grubenv` torn on vfat | L / M | guard unit, `.bak`, defaults (5.3) |
| ESP (vfat) damaged | L / H | rare, tiny writes; `fsck.fat` reported in `status`; recovery = USB reflash |
| a shared `/data` written by a newer slot breaks an older one | L / M | Docker-version warning; Docker stays on the kirkstone major |
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
| M2 | major | NBD is a module nothing loads; the streaming sandbox can't read a root-only TLS key | `CONFIG_BLK_DEV_NBD=y`; `rauc-stream` sandbox user with a group-readable key; cold-boot exit test (5.10, Phases 3/6) |
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
| RAUC's streaming client follows redirects, to `http` too, so a channel could send the board's requests elsewhere - its client certificate to another host, or GETs into the boat's own network (CodeRabbit) | RAUC streams from `boat-update`'s relay on `127.0.0.1`, which follows no redirect and passes on nothing but a 206 of the size asked for; a preflight also refuses a redirecting or Range-less channel before RAUC marks the slot unbootable |
| the metered-link check was made once, when a stream starts, and RAUC's client cannot be bound to a link: a route that moved to cellular mid-stream would be used (CodeRabbit) | the relay's connections are bound to the checked link (`SO_BINDTODEVICE`); if it goes away, the stream fails and the board stays on its slot |

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
