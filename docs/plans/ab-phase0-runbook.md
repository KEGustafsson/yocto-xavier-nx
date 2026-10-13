# Phase 0 runbook: what to check on the board before trusting the A/B design

Companion to [the A/B update plan](ab-os-updates.md), section 6 "Phase 0".
The plan rests on facts about this board that only the board can confirm.
This runbook is how to get them.

There are two parts:

1. **Read-only capture.** Safe on any board, including the boat.
   `scripts/ab-phase0-capture.sh` answers questions Q1–Q7 and Q9 by itself.
2. **Manual checks.** These change state, so do them on a **bench unit**
   with a USB-C cable and the host ready for a recovery reflash
   (`scripts/05-flash-nvme.sh`). Each step says what it changes and how to
   undo it.

Write the answers into the plan's section 11 "Your review" before Phase 1
code is flashed anywhere that matters.

## 1. Read-only capture

```bash
scripts/ab-phase0-capture.sh                         # the board in wol/boat.conf
scripts/ab-phase0-capture.sh root@192.168.0.43       # or name one
scripts/ab-phase0-capture.sh --channel-url https://<your-host>/<some-existing-file>
```

The transcript lands in `yocto/ab-phase0/`. The summary at the end maps to
the plan like this:

| Q | what it tells the plan | what changes if the answer is unexpected |
|---|---|---|
| Q1 | `root=` on the cmdline today | nothing for option A (GRUB writes its own); it matters for options B/C (5.2, 4) |
| Q2 | kernel in the rootfs or in `boot.img` | expected "no" today; Phase 1's `UBOOT_EXTLINUX = "1"` changes it |
| Q3 | ESP size and free space | GRUB + `grubenv` need about 2 MiB |
| Q4 | `L4TDefaultBootMode` | expected 1 (DIRECT). Phase 1's overlay and guard switch it to 0. |
| Q5 | NVIDIA rootfs redundancy level, retry max, slot-A status | expected `0/3/0`. A slot status of 255 means the sticky unbootable state is already set. |
| Q6 | kernel options for RAUC (loop, squashfs, dm-verity, nbd) | each missing one goes into `boat-rauc.cfg` (Phase 1/3) |
| Q7 | watchdog state with userspace up, and `kernel.panic` | input for 5.5 and D8 |
| Q9 | the OTA channel host answers Range requests | without `206`, there is no resumable download and no streaming (5.10, D13) |

## 2. Manual checks (bench unit only)

### 2.1 Does a kernel panic reset the board?

Changes: nothing persistent. `kernel.panic` is runtime only and reverts on
reboot.

```bash
echo 10 > /proc/sys/kernel/panic
echo c > /proc/sysrq-trigger      # deliberate crash
```

**Expected:** the board resets on its own within about 10 s and boots
again. Record the time to SSH. If it hangs instead, the reset path in 5.5
depends on the watchdog (2.2).

### 2.2 Does the hardware watchdog reset a board nobody is feeding?

Changes: nothing persistent.

```bash
cat /sys/class/watchdog/watchdog0/{identity,state,timeout}
systemctl status watchdog.service
kill -9 "$(pidof watchdog)"       # the device stays open, nobody feeds it
```

**Expected:** a reset after roughly `timeout` seconds. Record whether it
happens and how long it takes. If the daemon closes the device cleanly on
SIGKILL and nothing resets, write that down: it decides D8.

### 2.3 Is the watchdog running inside UEFI/GRUB, and before userspace?

This needs the Phase 1 GRUB build: `AB_LAYOUT=1` builds `grubaa64.efi`
(package `boat-grub-esp`). See 2.5 for the hand-off itself.

- **Inside GRUB.** Temporarily use a `grub.cfg` that only runs `sleep 900`.
  If the board resets on its own, the watchdog covers a GRUB hang. If it
  sits there, GRUB hangs need a power cycle (5.5 row "GRUB itself hangs").
- **Before userspace.** Boot with `init=/bin/sh` from the same GRUB
  command line and leave it. A reset means the watchdog runs before
  systemd. Stopping at the shell means it does not.

### 2.4 NVIDIA's rootfs retry counter: is it re-seeded by a cold power-on?

**Risk: real.** Get this wrong and the board ends up in the sticky
"unbootable" state (5.3), which needs the UEFI menu or a reflash. Do it on
the bench only.

1. `nvbootctrl -t rootfs dump-slots-info`. Note the retry count, if it is
   shown at all. If it is not shown, skip this check.
2. `systemctl mask nv_update_verifier.service`, so nothing resets the count.
3. Warm reboot (`reboot`) **twice**. The count should now read 1. **Stop
   here if it reads 0.** Run `systemctl unmask nv_update_verifier.service`
   and `nvbootctrl verify` immediately, before any further reboot.
4. **Cold** power cycle: pull the barrel jack for 10 s.
5. The count reads **2** → re-seeded on a cold boot (the plan's assumption
   in 5.3). It reads **0** → not re-seeded: unmask and verify **now**, and
   report it, because it changes the attempt budget (D10).
6. `systemctl unmask nv_update_verifier.service`, then
   `nvbootctrl verify`.

### 2.5 GRUB hand-off (the gating question for option A)

Changes: one UEFI variable (reverted at the end) and files on the ESP.

You need a `grubaa64.efi` and a test `grub.cfg` from the Phase 1 build, plus
`/boot/Image` and `/boot/initrd` on the rootfs. A Phase 1 image already has
them. On an older image, copy `Image` and the `tegra-minimal-initramfs`
`.cpio.gz` from the build's deploy directory.

```bash
# before: a fingerprint of the device tree the kernel got from UEFI
find /proc/device-tree -type f | sort | xargs sha256sum > /root/dt-before.txt

cp grubaa64.efi grub.cfg /boot/efi/EFI/BOOT/
V=/sys/firmware/efi/efivars/L4TDefaultBootMode-781e084c-a330-417c-b678-38e696380cb9
# Keep the variable exactly as it is now (the capture's Q4), so the revert
# puts back that value - or removes the variable if it was absent.
if [ -e "$V" ]; then cp "$V" /root/L4TDefaultBootMode.orig; chattr -i "$V"; else rm -f /root/L4TDefaultBootMode.orig; fi
# L4TDefaultBootMode = 0 (GRUB). efivarfs data is a 4-byte attribute word
# (0x7 = non-volatile + boot + runtime) followed by the u32 value.
printf '\x07\x00\x00\x00\x00\x00\x00\x00' > "$V"
reboot
```

After the reboot, check:

- [ ] the serial console shows "Attempting GRUB Boot", and `/proc/cmdline`
      carries GRUB's arguments, including `rauc.slot=`;
- [ ] `find /proc/device-tree -type f | sort | xargs sha256sum | diff - /root/dt-before.txt`
      is empty, or differs only in `/chosen` (bootargs, initrd addresses);
- [ ] HDMI/XFCE desktop, Wi-Fi, Bluetooth, fan and `nvpmodel` all work;
- [ ] `docker run --rm --runtime nvidia …` sees the GPU;
- [ ] `boat-sleep` puts it into SC7, and a magic packet (`wol/boat-wake.sh`)
      wakes it.

Revert, so the board goes back to its normal boot path:

```bash
V=/sys/firmware/efi/efivars/L4TDefaultBootMode-781e084c-a330-417c-b678-38e696380cb9
chattr -i "$V"
if [ -e /root/L4TDefaultBootMode.orig ]; then
    cat /root/L4TDefaultBootMode.orig > "$V"   # the value Q4 reported
else
    rm "$V"                                    # Q4 said absent: absent again
fi
rm /boot/efi/EFI/BOOT/grubaa64.efi /boot/efi/EFI/BOOT/grub.cfg
reboot
```

After that reboot, the capture's Q4 must read what it read before.

**If any box stays unchecked, option A is in question.** Bring the
findings back to the plan (section 4, option C).

### 2.6 Where the UEFI menu resets a rootfs slot

There is no risk in looking. On the serial console (115200 8N1), press
**Esc** during the NVIDIA logo, then find the menu that shows or resets
the rootfs slot status. On R35 it is under the L4T configuration pages of
the Device Manager. Write down the exact path: it becomes the
sticky-recovery procedure in docs/06 (plan 5.3, test 28).
