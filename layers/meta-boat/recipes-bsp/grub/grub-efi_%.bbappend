FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# A/B OS updates (docs/plans/ab-os-updates.md 5.1, 5.3). grub-efi is only
# built for an A/B image - boat-grub-esp copies its grubaa64.efi onto the
# ESP, where L4TLauncher hands off to it - so neither change below touches a
# single-slot build.

# Keep the UEFI watchdog armed through GRUB instead of cancelling it, so a
# hang inside GRUB still ends in a reset and a fallback to the other slot.
SRC_URI += "file://0001-efi-re-arm-the-UEFI-watchdog-instead-of-disabling-it.patch"

# What boat-grub-esp's grub.cfg uses beyond poky's default set:
#   regexp  - take the disk from the ESP's own $root, so the slots are
#             addressed on the same drive GRUB was loaded from
#   echo    - say on the console which slot it chose and why
#   sleep   - a moment for that message before a reset
#   reboot  - every failure path ends in a reset, never a prompt
GRUB_BUILDIN:append = " regexp echo sleep reboot"
