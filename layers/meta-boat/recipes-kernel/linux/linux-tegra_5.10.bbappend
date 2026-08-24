FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Docker/OCI support + local device passthrough for the container-host
# design (docs/05-phase2-boat-computer-layer.md). Without these, dockerd
# fails to start (missing cgroups/overlayfs) or containers can't reach
# /dev/i2c-*, /dev/spidev*, or USB-serial adapters.
SRC_URI += "file://boat-docker.cfg"

# Suspend-to-RAM, so the board can be put into SC7 remotely and woken with a
# Wake-on-LAN magic packet (boat-power). meta-tegra's defconfig is expected
# to have CONFIG_SUSPEND already - the fragment states it rather than
# assuming it, and adds the PM debug knobs worth having on a board that has
# to come back from sleep unattended.
SRC_URI += "file://boat-power.cfg"
