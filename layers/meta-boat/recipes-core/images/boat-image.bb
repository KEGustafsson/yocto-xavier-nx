SUMMARY = "Boat / marine computer image for Jetson Xavier NX (NVMe boot)"
LICENSE = "MIT"

# Start from a console-only base with the standard command-line tooling.
require recipes-core/images/core-image-base.bb

IMAGE_FEATURES += "ssh-server-openssh"

IMAGE_INSTALL:append = " \
    packagegroup-boat \
    boat-docker-config \
    boat-hmi-autostart \
    boat-compose \
    kernel-modules \
    "

# USB-serial and other local-sensor adapters a containerized app might need
# passed through (docs/05 "Local device passthrough into containers").
# CAN kernel modules dropped: NMEA 2000/CAN is provided by an external
# interface now, not this host (docs/05 "What changed from the earlier
# scaffold").
MACHINE_ESSENTIAL_EXTRA_RRECOMMENDS += " \
    kernel-module-usb-serial \
    "

# Give the rootfs headroom for Docker's local image cache, logs, and compose
# state before /data (docs/05 "Reliability") is provisioned and dockerd's
# data-root actually moves there. Bump if `docker pull` starts failing ENOSPC.
IMAGE_ROOTFS_EXTRA_SPACE = "4194304"

# Reproducible, serviceable systemd system.
# NOTE: "virtualization wayland opengl" (docker/nvidia-container-toolkit's
# REQUIRED_DISTRO_FEATURES + Weston) live in local.conf, not here -
# DISTRO_FEATURES is evaluated per-recipe at parse time against the global
# distro config, so an image-recipe-local append can't retroactively
# unskip another recipe that already parsed as "missing required distro
# feature". See scripts/02-configure-build.sh.
DISTRO_FEATURES:append = " systemd"

# Login user for the console/Weston session (docs/05 "Boot flow for the
# display"). Fixed scaffold user for now, locked password (console-autologin
# + SSH key only) - replace with docs/05's interactive build-time user-
# creation flow (not yet implemented in scripts/02-configure-build.sh) when
# that lands; must keep matching boat-hmi-autostart's BOAT_HMI_USER/_UID.
inherit extrausers
# i2c/spi groups: unlike video/render/input/dialout, no recipe on this
# kirkstone snapshot creates them (it's a Debian/Raspbian convention, not
# something i2c-tools/spi-tools provisions here) - create them explicitly so
# the usermod below has something to add "boat" to. Note this only grants
# group membership; actual /dev/i2c-*, /dev/spidev* device-node group
# ownership still needs udev rules, not wired up yet.
EXTRA_USERS_PARAMS = "\
    groupadd -f i2c; \
    groupadd -f spi; \
    useradd -u 2000 -m -s /bin/bash -p '*' boat; \
    usermod -a -G video,render,input,dialout,i2c,spi,docker boat; \
    usermod -s /bin/bash root; \
"

# NetworkManager (packagegroup-boat-connectivity) is this image's network
# manager, not systemd-networkd - but the base systemd package still ships
# and enables systemd-networkd(-wait-online).service regardless. Left
# unmasked, systemd-networkd-wait-online.service has nothing it ever
# manages to wait for, so it burns its full 120s default timeout on every
# single boot before failing. CONFIRMED ON HARDWARE: network-online.target
# (which gates docker.service, and in turn boat-compose.service) isn't
# reached until that timeout finally expires, delaying container start by
# ~110s every boot for no reason - masking it (equivalent to `systemctl
# mask`) cuts docker+compose startup from ~135s to ~22s after boot.
mask_unused_networkd() {
    install -d ${IMAGE_ROOTFS}${sysconfdir}/systemd/system
    for u in systemd-networkd.service systemd-networkd-wait-online.service; do
        ln -sf /dev/null ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/$u
    done
}
ROOTFS_POSTPROCESS_COMMAND += "mask_unused_networkd; "
