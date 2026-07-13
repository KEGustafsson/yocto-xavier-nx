SUMMARY = "Docker daemon config for a Jetson container host"
DESCRIPTION = "Sets the NVIDIA runtime as Docker's default (so every \
compose file gets GPU/DLA access without an explicit 'runtime:' key) and \
moves Docker's data-root off the small rootfs onto the NVMe /data \
partition. See docs/05-phase2-boat-computer-layer.md."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://daemon.json"

# nvidia-container-toolkit provides the "nvidia-container-runtime" binary
# this config points at; docker-ce is the daemon that reads it.
RDEPENDS:${PN} = "docker-ce nvidia-container-toolkit"

S = "${WORKDIR}"

do_install() {
    install -d ${D}${sysconfdir}/docker
    install -m 0644 ${WORKDIR}/daemon.json ${D}${sysconfdir}/docker/daemon.json
}

FILES:${PN} = "${sysconfdir}/docker/daemon.json"

# /data is a separate partition mounted by the reliability/data-partition
# wiring (docs/05 "Reliability"); this recipe only writes the config that
# points dockerd at it; it does not create the mount itself.
