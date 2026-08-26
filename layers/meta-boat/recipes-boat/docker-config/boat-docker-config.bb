SUMMARY = "Docker daemon config for a Jetson container host"
DESCRIPTION = "Sets the NVIDIA runtime as Docker's default (so every \
compose file gets GPU/DLA access without an explicit 'runtime:' key) and \
moves Docker's data-root off the small rootfs onto the NVMe /data \
partition. See docs/05-phase2-boat-computer-layer.md."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://daemon.json \
           file://10-data-root.conf \
"

# nvidia-container-toolkit provides the "nvidia-container-runtime" binary
# this config points at; docker-ce is the daemon that reads it.
RDEPENDS:${PN} = "docker-ce nvidia-container-toolkit"

S = "${WORKDIR}"

# Two config files and nothing compiled - no reason to build a separate,
# arch-specific package per MACHINE. Matches the other config/script-only
# recipes in this layer.
inherit allarch

do_install() {
    install -d ${D}${sysconfdir}/docker
    install -m 0644 ${WORKDIR}/daemon.json ${D}${sysconfdir}/docker/daemon.json

    install -d ${D}${systemd_system_unitdir}/docker.service.d
    install -m 0644 ${WORKDIR}/10-data-root.conf \
        ${D}${systemd_system_unitdir}/docker.service.d/10-data-root.conf
}

FILES:${PN} = "\
    ${sysconfdir}/docker/daemon.json \
    ${systemd_system_unitdir}/docker.service.d/10-data-root.conf \
"

# Meant to be edited on the boat - the data-root path and the log limits are
# both site decisions. Without CONFFILES those edits are silently reverted by a
# package upgrade. (boat-power.bb does the same for its two config files.)
CONFFILES:${PN} = "${sysconfdir}/docker/daemon.json"

# /data is NOT created by this recipe, and as of today nothing else in the
# layer creates it either: there is no data.mount and no fstab entry. What
# happens right now is that dockerd creates /data/docker on the 16 GiB flashed
# rootfs - which is why boat-image.bb carries IMAGE_ROOTFS_EXTRA_SPACE, and why
# the log-opts above are not optional. Provisioning /data as its own partition
# is still open work (docs/05 "Reliability"); the drop-in installed above is
# what makes dockerd wait for it rather than race it on the day it lands.
