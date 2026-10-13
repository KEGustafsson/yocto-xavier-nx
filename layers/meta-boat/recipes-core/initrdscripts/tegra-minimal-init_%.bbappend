FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# A/B OS updates (docs/plans/ab-os-updates.md 5.2, 5.5). For an A/B image the
# initramfs mounts exactly the slot GRUB chose (root=<disk>p<N>, checked
# against the GPT entry name), and resets the board if it cannot - instead
# of meta-tegra's label search, mmcblk0p1 fallback and mount loop that never
# gives up. Single-slot builds keep meta-tegra's scripts unchanged.
SRC_URI += "\
    file://boat-ab-init-boot.sh \
    file://boat-ab-platform-preboot.sh \
"

do_install:append() {
    if [ "${BOAT_AB_LAYOUT}" = "1" ]; then
        install -m 0755 ${WORKDIR}/boat-ab-init-boot.sh ${D}/init
        sed -e 's|@DISK@|${BOAT_AB_DISK}|g' \
            -e 's|@PARTNUM_A@|${BOAT_AB_PARTNUM_APP_A}|g' \
            -e 's|@PARTNUM_B@|${BOAT_AB_PARTNUM_APP_B}|g' \
            ${WORKDIR}/boat-ab-platform-preboot.sh > ${D}${sysconfdir}/platform-preboot
        chmod 0644 ${D}${sysconfdir}/platform-preboot
    fi
}
do_install[vardeps] += "BOAT_AB_LAYOUT BOAT_AB_DISK BOAT_AB_PARTNUM_APP_A BOAT_AB_PARTNUM_APP_B"
