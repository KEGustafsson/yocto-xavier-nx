# A/B OS updates (docs/plans/ab-os-updates.md 5.2). With BOAT_AB_LAYOUT on,
# the external (NVMe) partition layout is not one of NVIDIA's files: it is
# GENERATED from NVIDIA's own flash_l4t_nvme_rootfs_ab.xml by
# boat-ab-layout.py, which leaves APP_b unwritten at flash time, appends the
# SWAP and fill-to-end DATA partitions, and pins the GPT entry numbers the
# boot and update path address (BOAT_AB_PARTNUM_* in conf/layer.conf).
# Generating it keeps NVIDIA's layout out of this repository and lets a BSP
# update's own changes flow through; the script fails the build if the
# template stops having the shape it expects.
#
# Deliberately NOT USE_REDUNDANT_FLASH_LAYOUT: that also installs the
# RootfsRedundancyLevel=1 UEFI overlay, which ties the rootfs slot to the
# firmware boot chain - and this board's chain B is incomplete (plan
# section 3) - and it derives an internal template name,
# flash_l4t_t194_qspi_p3668_rootfs_ab.xml, that R35.6.4 does not ship.
#
# The script is run from this layer rather than fetched: tegra-bootfiles
# shares NVIDIA's unpacked BSP (tegra-shared-binaries.inc deletes do_unpack
# and empties SRC_URI), so nothing in SRC_URI would ever reach WORKDIR.
BOAT_AB_LAYOUT_SCRIPT := "${THISDIR}/files/boat-ab-layout.py"

BOAT_AB_LAYOUT_TEMPLATE ?= "${S}/tools/kernel_flash/flash_l4t_nvme_rootfs_ab.xml"

# meta-tegra installs ${PARTITION_FILE_EXTERNAL} under the name
# ${PARTITION_LAYOUT_EXTERNAL}; point it at the generated file. The non-A/B
# value is meta-tegra's own default, restated because this assignment
# replaces it.
PARTITION_FILE_EXTERNAL = "${@'${WORKDIR}/${PARTITION_LAYOUT_EXTERNAL}' if d.getVar('BOAT_AB_LAYOUT') == '1' else '${S}/tools/kernel_flash/${PARTITION_LAYOUT_EXTERNAL}'}"

do_boat_ab_layout() {
    python3 ${BOAT_AB_LAYOUT_SCRIPT} \
        ${BOAT_AB_LAYOUT_TEMPLATE} ${WORKDIR}/${PARTITION_LAYOUT_EXTERNAL} \
        ${BOAT_AB_SWAP_SIZE} ${BOAT_AB_DATA_MIN_SIZE} \
        "APP=${BOAT_AB_PARTNUM_APP_A},APP_b=${BOAT_AB_PARTNUM_APP_B},esp=${BOAT_AB_PARTNUM_ESP},SWAP=${BOAT_AB_PARTNUM_SWAP},DATA=${BOAT_AB_PARTNUM_DATA}"
}
# Rerun when the script changes, not only when its arguments do.
do_boat_ab_layout[file-checksums] += "${BOAT_AB_LAYOUT_SCRIPT}:True"

python () {
    if d.getVar('BOAT_AB_LAYOUT') != '1':
        return
    # The generated file is installed under PARTITION_LAYOUT_EXTERNAL. Any
    # other name would quietly replace one of NVIDIA's layouts with ours.
    if d.getVar('PARTITION_LAYOUT_EXTERNAL') != 'boat-nvme-ab.xml':
        bb.fatal('BOAT_AB_LAYOUT = "1" needs PARTITION_LAYOUT_EXTERNAL = "boat-nvme-ab.xml" '
                 '(scripts/02-configure-build.sh with AB_LAYOUT=1, or kas/ab-updates.yml, sets both)')
    # After do_configure, which waits for tegra-binaries to unpack the BSP
    # the template comes from.
    bb.build.addtask('do_boat_ab_layout', 'do_install', 'do_configure', d)
}
