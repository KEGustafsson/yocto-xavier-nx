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
# It also records how this boot got here in /run/boat-ab/boot-path: "grub"
# (rauc.slot= is on the command line) or "outside-grub" - L4TLauncher's own
# fallback, which always boots slot A and bypasses the slot logic. The
# update tooling refuses to install anything in that state.
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

mode="$(efi_u32 L4TDefaultBootMode)"
if [ "$mode" = 0 ]; then
    exit 0
fi
warn "L4TDefaultBootMode is '$mode', not 0 (GRUB) - setting it so the next boot goes through GRUB"
command -v efivar >/dev/null 2>&1 || die "efivar is not installed - cannot fix L4TDefaultBootMode"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '\000\000\000\000' > "$tmp"
efivar -w -n "${BOAT_AB_NV_GUID}-L4TDefaultBootMode" -f "$tmp"
mode="$(efi_u32 L4TDefaultBootMode)"
[ "$mode" = 0 ] || die "L4TDefaultBootMode still reads '$mode' after writing 0"
say "L4TDefaultBootMode is now 0 (GRUB)"
