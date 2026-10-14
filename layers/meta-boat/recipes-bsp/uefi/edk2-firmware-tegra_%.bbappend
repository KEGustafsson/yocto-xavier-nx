FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# "_%" rather than the pinned "_35.6.4" this used to carry. scripts/01-fetch-layers.sh
# does `git reset --hard origin/kirkstone` on every run, so the moment OE4T
# bumps L4T the versioned recipe is renamed and a version-pinned bbappend
# becomes a DANGLING append - which bitbake reports as a hard parse ERROR, not
# a warning, and which then blocks every target rather than just this one. The
# patch below guards preprocessor-only code and is not version-specific, so
# there is nothing for the pin to protect.

# DeviceTree.inf preprocesses .dts files with "cpp -x assembler-with-cpp
# -undef", a mode where GCC's preprocessor can't parse
# __has_feature(...)/__has_builtin(...) as callable operators even
# though defined(__has_feature) reports true - MdePkg/Include/Base.h's
# clang-compat fallbacks (dead code for our GCC-only build) hit that
# and fail do_compile with "missing binary operator before token '('".
SRC_URI += "file://0001-Base.h-guard-has_feature-has_builtin-for-assembler-.patch"

# A/B OS updates (docs/plans/ab-os-updates.md 5.1). L4TLauncher only hands
# off to GRUB (\EFI\BOOT\grubaa64.efi) when the UEFI variable
# L4TDefaultBootMode is 0; NVIDIA's L4TConfiguration overlay seeds it as 1,
# DIRECT (extlinux). With BOAT_AB_LAYOUT on, rewrite that one property in the
# built overlay. The overlay only SEEDS a variable that does not exist yet,
# so a board that already carries 1 is put right at runtime by
# boat-bootmode-guard (recipes-boat/ab-boot).
#
# The same overlay boots USB first: DefaultBootPriority "usb,nvme,..." sorts
# the boot order once, on the first boot, and NewDeviceHierarchy = 1 puts
# every USB stick or SD card inserted later at the front. A bootable stick
# left in would then boot instead of the NVMe's L4TLauncher and GRUB - the
# slot logic would never run. NVMe first, and new devices at the end
# (plan 5.2). boat-bootmode-guard also sets NewDeviceHierarchy at runtime.
BOAT_AB_UEFI_VARS = "/fragment@0/__overlay__/firmware/uefi/variables"
BOAT_AB_BOOTMODE_NODE = "${BOAT_AB_UEFI_VARS}/gNVIDIAPublicVariableGuid/L4TDefaultBootMode"
BOAT_AB_HIERARCHY_NODE = "${BOAT_AB_UEFI_VARS}/gNVIDIAPublicVariableGuid/NewDeviceHierarchy"
BOAT_AB_PRIORITY_NODE = "${BOAT_AB_UEFI_VARS}/gNVIDIATokenSpaceGuid/DefaultBootPriority"
BOAT_AB_BOOT_PRIORITY = "nvme,emmc,sd,usb,ufs"

do_compile:append() {
    if [ "${BOAT_AB_LAYOUT}" = "1" ]; then
        dtbo=${B}/images/L4TConfiguration.dtbo
        fdtput -t bx $dtbo ${BOAT_AB_BOOTMODE_NODE} data 00 00 00 00
        mode="$(fdtget -t bx $dtbo ${BOAT_AB_BOOTMODE_NODE} data)"
        [ "$mode" = "0 0 0 0" ] || bbfatal "L4TDefaultBootMode is '$mode' after fdtput, expected 0 (GRUB)"
        fdtput -t bx $dtbo ${BOAT_AB_HIERARCHY_NODE} data 00
        hierarchy="$(fdtget -t bx $dtbo ${BOAT_AB_HIERARCHY_NODE} data)"
        [ "$hierarchy" = "0" ] || bbfatal "NewDeviceHierarchy is '$hierarchy' after fdtput, expected 0"
        fdtput -t s $dtbo ${BOAT_AB_PRIORITY_NODE} data "${BOAT_AB_BOOT_PRIORITY}"
        priority="$(fdtget -t s $dtbo ${BOAT_AB_PRIORITY_NODE} data)"
        [ "$priority" = "${BOAT_AB_BOOT_PRIORITY}" ] \
            || bbfatal "DefaultBootPriority is '$priority' after fdtput, expected '${BOAT_AB_BOOT_PRIORITY}'"
    fi
}
do_compile[vardeps] += "BOAT_AB_LAYOUT BOAT_AB_BOOT_PRIORITY"
