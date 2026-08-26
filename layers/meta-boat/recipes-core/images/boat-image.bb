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
    boat-power \
    boat-grow-rootfs \
    boat-firefox \
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
# NOTE: "virtualization x11 opengl pam" (docker/nvidia-container-toolkit's
# REQUIRED_DISTRO_FEATURES + the Xorg/XFCE helm desktop) live in local.conf,
# not here -
# DISTRO_FEATURES is evaluated per-recipe at parse time against the global
# distro config, so an image-recipe-local append can't retroactively
# unskip another recipe that already parsed as "missing required distro
# feature". See scripts/02-configure-build.sh.
DISTRO_FEATURES:append = " systemd"

# Login user for the console/XFCE session (docs/05 "Boot flow for the
# display"). Fixed scaffold user for now - replace with docs/05's interactive
# build-time user-creation flow (not yet implemented in
# scripts/02-configure-build.sh) when that lands; must keep matching
# boat-hmi-autostart's BOAT_HMI_USER/_UID.
#
# -p '' gives "boat" an EMPTY password, not a locked one ('*', which is what
# this used to be). Empty means the account is usable and `passwd` can set a
# real one on the boat; locked meant no string could ever authenticate, so
# the account was reachable only through the tty1 autologin.
#
# SECURITY, and it is not subtle: an empty password combines badly with two
# other things this image currently has. IMAGE_FEATURES still carries poky's
# stock "debug-tweaks", whose ssh_allow_empty_password sets
# PermitEmptyPasswords yes - and /etc/sudoers.d/boat grants this user
# passwordless sudo. Together that is unauthenticated root over the network
# for anyone who can reach port 22. Fine on a bench; wrong the moment the
# board is on a marina LAN. Close it by either setting a password on first
# boot (`passwd boat`) or dropping debug-tweaks from EXTRA_IMAGE_FEATURES -
# console autologin and sudo keep working without it.
inherit extrausers
# i2c/spi groups: unlike video/render/input/dialout, no recipe on this
# kirkstone snapshot creates them (it's a Debian/Raspbian convention, not
# something i2c-tools/spi-tools provisions here) - create them explicitly so
# the usermod below has something to add "boat" to. Note this only grants
# group membership; actual /dev/i2c-*, /dev/spidev* device-node group
# ownership still needs udev rules, not wired up yet.
#
# The explicit -g matters. Without it groupadd takes the first free id at or
# above GID_MIN, which put i2c at gid 1000 and spi at 1001. Container images
# overwhelmingly run their app as uid/gid 1000 (node, ubuntu, debian, most
# -slim bases), and with no userns-remap in daemon.json a container writing
# to a bind-mounted volume stores those raw numeric ids. The host then
# rendered such files as "1000:i2c" - CONFIRMED ON HARDWARE - which is both
# confusing and wrong in substance: every file a container created ended up
# group-owned by a hardware-access group, so anything later given i2c
# membership to reach /dev/i2c-* would also get group access to all of it.
# Pinning both into the system range leaves 1000/1001 unclaimed, so such
# files show up as a plain "1000:1000" - honestly labelled as a container's.
# NOT retroactive: files already written under gid 1000 need a chgrp sweep.
EXTRA_USERS_PARAMS = "\
    groupadd -f -g 990 i2c; \
    groupadd -f -g 989 spi; \
    useradd -u 2000 -m -s /bin/bash -p '' boat; \
    usermod -a -G video,render,input,dialout,i2c,spi,docker,jtop boat; \
    usermod -s /bin/bash root; \
"
# jtop group: CONFIRMED ON HARDWARE - without it, jtop (from
# python3-jetson-stats, packagegroup-boat-jetson) fails for the "boat"
# user with "I can't access jtop.service. Please logout or reboot this
# board" - jtop.service's socket (/run/jtop.sock) is root:jtop
# srw-rw----, and nothing added "boat" to that group by default. Like
# the "docker" group above, "jtop" itself is created by its owning
# package's (python3-jetson-stats) own postinstall, not by this recipe -
# it already exists by the time this usermod runs.

# Let the console/desktop user reach root. Without this the image is a dead
# end: "boat" has a locked password ('*' above), so `su` cannot be satisfied
# either, and the only reason root is reachable at all today is that
# EXTRA_IMAGE_FEATURES still carries poky's stock "debug-tweaks" - which
# leaves root with an EMPTY password and, via ssh_allow_empty_password +
# ssh_allow_root_login, lets that empty password in over SSH as well. That is
# fine on a bench and wrong on a boat; this rule is what makes it safe to drop
# debug-tweaks.
#
# NOPASSWD is not laziness - it is forced: the account's password is locked,
# so any password prompt would be unsatisfiable. The grant is no wider than
# what the login already implies, since tty1 autologs "boat" in and that user
# is in the "docker" group, which is root-equivalent by design (a container
# can bind-mount /). Physical access to the helm display is already root.
#
# Written here rather than in a recipe of its own because the user this
# grants to is created here, a few lines up - the two want to stay together.
# The @includedir line is appended only if sudo's own /etc/sudoers does not
# already have one, so this works whether or not the shipped default carries
# it.
BOAT_HMI_USER ?= "boat"
install_boat_sudoers() {
    install -d -m 0750 ${IMAGE_ROOTFS}${sysconfdir}/sudoers.d
    # Remove first: the file is left mode 0440 below, so a second pass over
    # an existing rootfs would fail to rewrite it - silently, since a failed
    # redirection here would not stop the rest of the postprocess.
    rm -f ${IMAGE_ROOTFS}${sysconfdir}/sudoers.d/boat
    cat > ${IMAGE_ROOTFS}${sysconfdir}/sudoers.d/boat <<EOF
# Installed by boat-image.bb. The account's password is locked, so this has
# to be NOPASSWD to be usable at all.
${BOAT_HMI_USER} ALL=(ALL) NOPASSWD: ALL
EOF
    # sudo refuses to read a drop-in that is group- or world-writable.
    chmod 0440 ${IMAGE_ROOTFS}${sysconfdir}/sudoers.d/boat
    if ! grep -qE '^[@#]includedir[[:space:]]+/etc/sudoers\.d' \
        ${IMAGE_ROOTFS}${sysconfdir}/sudoers 2>/dev/null; then
        echo '@includedir /etc/sudoers.d' >> ${IMAGE_ROOTFS}${sysconfdir}/sudoers
    fi
}
ROOTFS_POSTPROCESS_COMMAND += "install_boat_sudoers; "

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
