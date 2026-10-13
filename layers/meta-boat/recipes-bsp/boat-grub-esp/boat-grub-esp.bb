SUMMARY = "GRUB on the ESP: A/B rootfs slot selection for the boat computer"
DESCRIPTION = "Puts grubaa64.efi, its grub.cfg and the initial grubenv into \
the EFI system partition. L4TLauncher hands off to \\EFI\\BOOT\\grubaa64.efi \
when L4TDefaultBootMode is 0 (GRUB), and this grub.cfg picks the rootfs slot \
- one attempt for a slot on trial, two for a committed one - and boots its \
kernel and initrd from /boot. Only built for an A/B image (BOAT_AB_LAYOUT); \
see docs/plans/ab-os-updates.md sections 5.1 and 5.3."
# grub.cfg and the packaging are this layer's (MIT); grubaa64.efi is GRUB's.
LICENSE = "MIT & GPL-3.0-only"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302 \
                    file://${COMMON_LICENSE_DIR}/GPL-3.0-only;md5=c79ff39f19dfec6d293b95dea7b07891"

SRC_URI = "file://grub.cfg.in"

require conf/image-uefi.conf

COMPATIBLE_MACHINE = "(tegra)"

# grub-native for grub-script-check and grub-editenv at build time; grub-efi
# for the EFI binary itself, which it deploys rather than packages in a form
# the ESP image can use.
DEPENDS = "grub-native"
do_install[depends] += "grub-efi:do_deploy"

S = "${WORKDIR}"
B = "${WORKDIR}/build"

# grub-efi deploys "grub-efi-boot<arch>.efi" when it is not the EFI_PROVIDER
# (L4TLauncher is), see poky's grub-efi_2.06.bb.
GRUB_EFI_DEPLOYED = "grub-efi-${EFI_BOOT_IMAGE}"

do_compile() {
    sed -e 's|@KERNEL_ARGS@|${KERNEL_ARGS}|g' \
        -e 's|@DISK@|${BOAT_AB_DISK}|g' \
        -e 's|@PARTNUM_A@|${BOAT_AB_PARTNUM_APP_A}|g' \
        -e 's|@PARTNUM_B@|${BOAT_AB_PARTNUM_APP_B}|g' \
        ${S}/grub.cfg.in > ${B}/grub.cfg
    if grep -n '@[A-Z_]*@' ${B}/grub.cfg; then
        bbfatal "grub.cfg still has an unsubstituted placeholder (above)"
    fi
    # A grub.cfg GRUB cannot parse would drop the board to a prompt with no
    # fallback - catch it here, with the GRUB version that will run it.
    grub-script-check ${B}/grub.cfg

    # The initial state: the flashed slot A is committed; slot B was not
    # written by the flash (boat-ab-layout.py) and stays unusable until the
    # first update fills it. Every reflash starts from here again.
    rm -f ${B}/grubenv
    grub-editenv ${B}/grubenv create
    grub-editenv ${B}/grubenv set ORDER="A B" \
        A_OK=1 A_TRY=0 A_COMMITTED=1 \
        B_OK=0 B_TRY=0 B_COMMITTED=0 \
        BOAT_STAY=0
    cp ${B}/grubenv ${B}/grubenv.bak
}
do_compile[vardeps] += "KERNEL_ARGS BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B"

do_install() {
    install -d ${D}${EFIDIR}
    install -m 0644 ${DEPLOY_DIR_IMAGE}/${GRUB_EFI_DEPLOYED} ${D}${EFIDIR}/grubaa64.efi
    install -m 0644 ${B}/grub.cfg ${D}${EFIDIR}/grub.cfg
    install -m 0644 ${B}/grubenv ${D}${EFIDIR}/grubenv
    install -m 0644 ${B}/grubenv.bak ${D}${EFIDIR}/grubenv.bak
}

FILES:${PN} = "${EFIDIR}"
# The EFI binary is a firmware payload for the ESP, not a target userspace
# binary - the usual ELF checks do not apply to it.
INSANE_SKIP:${PN} += "arch"
PACKAGE_ARCH = "${MACHINE_ARCH}"
