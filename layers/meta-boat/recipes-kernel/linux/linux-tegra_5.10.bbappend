FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Docker/OCI support + local device passthrough for the container-host
# design (docs/05-phase2-boat-computer-layer.md). Without these, dockerd
# fails to start (missing cgroups/overlayfs) or containers can't reach
# /dev/i2c-*, /dev/spidev*, or USB-serial adapters.
SRC_URI += "file://boat-docker.cfg"
