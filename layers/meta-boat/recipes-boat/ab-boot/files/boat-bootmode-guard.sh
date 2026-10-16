#!/bin/sh
# Keep L4TLauncher handing off to GRUB, and say so when it did not.
#
# L4TLauncher only starts \EFI\BOOT\grubaa64.efi - and with it the A/B slot
# selection - when the UEFI variable L4TDefaultBootMode is 0. The image's
# L4TConfiguration overlay seeds it as 0, but an overlay only seeds a
# variable that does not exist yet: a board flashed before A/B, or one whose
# UEFI settings were reset from the setup menu, carries 1 (extlinux) and
# would boot slot A every time, ignoring grubenv. This puts it back.
#
# It also keeps UEFI from putting a newly inserted USB stick or SD card at
# the front of its boot order (NewDeviceHierarchy, seeded as 1 by NVIDIA's
# overlay): a bootable stick left in would otherwise be booted instead of
# the NVMe - and GRUB, which counts the attempts, would never run. A failure
# there is only a warning: this unit is one the trial's health gate waits
# for.
#
# It also records how this boot got here in /run/boat-ab/boot-path: "grub"
# (rauc.slot= is on the command line) or "outside-grub" - L4TLauncher's own
# fallback, which always boots slot A and bypasses the slot logic. The
# update tooling refuses to install anything in that state. Outside GRUB
# with L4TDefaultBootMode already 0, the setting was not the cause: GRUB
# itself did not load (grubaa64.efi unreadable, or UEFI could not write the
# ESP), every reboot comes back the same way, and that is an alert.
#
# See docs/plans/ab-os-updates.md section 5.1.
set -eu
SELF=boat-bootmode-guard
# shellcheck source=SCRIPTDIR/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

mkdir -p "$BOAT_AB_RUNDIR"
if [ -n "$(booted_slot)" ]; then
    echo grub > "$BOAT_AB_RUNDIR/boot-path"
    say "booted slot $(booted_slot) through GRUB"
else
    echo outside-grub > "$BOAT_AB_RUNDIR/boot-path"
    warn "this boot did not come through GRUB (no rauc.slot= on the command line):"
    warn "L4TLauncher booted slot A by itself, so A/B slot selection is bypassed"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Write $2 zero bytes to gNVIDIAPublicVariableGuid variable $1. efivar keeps
# the variable's attributes and handles efivarfs's immutable flag.
efi_write_zero() {
    command -v efivar >/dev/null 2>&1 || { warn "efivar is not installed"; return 1; }
    head -c "$2" /dev/zero > "$tmp" && efivar -w -n "${BOAT_AB_NV_GUID}-$1" -f "$tmp"
}

hierarchy="$(efi_u8 NewDeviceHierarchy)"
case "$hierarchy" in
    0|absent) ;;
    *)
        warn "NewDeviceHierarchy is '$hierarchy': UEFI puts every newly inserted USB stick or SD card first in its boot order - setting it to 0"
        if efi_write_zero NewDeviceHierarchy 1 && [ "$(efi_u8 NewDeviceHierarchy)" = 0 ]; then
            say "NewDeviceHierarchy is now 0: new boot devices go to the end of UEFI's boot order"
        else
            warn "could not set NewDeviceHierarchy to 0 - take any bootable USB stick or SD card out before a reboot"
        fi
        ;;
esac

mode="$(efi_u32 L4TDefaultBootMode)"
if [ "$mode" = 0 ]; then
    if [ -z "$(booted_slot)" ]; then
        alert "this boot did not come through GRUB although L4TDefaultBootMode is 0 (GRUB): GRUB itself did not load - grubaa64.efi is unreadable, or UEFI could not write the ESP. Rebooting will not help: reflash at the dock. Slot A is running, uncounted, whatever grubenv says - it may be an older or rejected release"
    fi
    exit 0
fi
warn "L4TDefaultBootMode is '$mode', not 0 (GRUB) - setting it so the next boot goes through GRUB"
efi_write_zero L4TDefaultBootMode 4 || die "cannot fix L4TDefaultBootMode"
mode="$(efi_u32 L4TDefaultBootMode)"
[ "$mode" = 0 ] || die "L4TDefaultBootMode still reads '$mode' after writing 0"
say "L4TDefaultBootMode is now 0 (GRUB)"
