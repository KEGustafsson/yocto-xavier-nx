SUMMARY = "Marine embedded computer image for Jetson Xavier NX (NVMe boot)"
LICENSE = "MIT"

# Start from a console-only base with the standard command-line tooling.
require recipes-core/images/core-image-base.bb

IMAGE_FEATURES += "ssh-server-openssh"

IMAGE_INSTALL:append = " \
    packagegroup-boat \
    boat-can-setup \
    kernel-modules \
    "

# CAN / SocketCAN, USB-serial and common GNSS/USB adapters need these kernel
# modules; meta-tegra ships them as loadable modules. Pull them into the rootfs.
MACHINE_ESSENTIAL_EXTRA_RRECOMMENDS += " \
    kernel-module-can \
    kernel-module-can-raw \
    kernel-module-can-dev \
    kernel-module-mttcan \
    "

# Give the rootfs some headroom for logs, charts and npm-installed apps.
IMAGE_ROOTFS_EXTRA_SPACE = "2097152"

# Reproducible, serviceable systemd system.
DISTRO_FEATURES:append = " systemd"
