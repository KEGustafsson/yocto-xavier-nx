SUMMARY = "Boat / marine computer image for Jetson Xavier NX (NVMe boot)"
LICENSE = "MIT"

# Start from a console-only base with the standard command-line tooling.
require recipes-core/images/core-image-base.bb

IMAGE_FEATURES += "ssh-server-openssh"

# Both root and "boat" log in with an EMPTY password, deliberately, and both
# halves of that are now stated here rather than inherited by accident.
#
# "boat" gets its empty password from useradd -p '' further down. root's used
# to come only from poky's stock EXTRA_IMAGE_FEATURES ?= "debug-tweaks" in
# local.conf - a line this project does not manage, copied in from poky's
# sample - which happens to suppress the zap_empty_root_password rootfs
# postprocess. Anything that dropped debug-tweaks would have silently locked
# root instead, which is not a thing that should happen by side effect.
#
# The three features named here are the specific ones that keep this working,
# unbundled from the rest of debug-tweaks:
#   empty-root-password  - do not run zap_empty_root_password, so root's
#                          shadow field stays empty rather than becoming '*'
#   allow-empty-password - sshd PermitEmptyPasswords yes
#   allow-root-login     - sshd PermitRootLogin yes; without it sshd falls
#                          back to prohibit-password and root cannot log in
#                          over the network at all, password or not
#
# What this means in practice: anyone who can reach port 22 can log in as
# root without a credential, and "boat" additionally has passwordless sudo.
# That is a deliberate choice for a bench/development image. Reverse it by
# removing these three (and debug-tweaks) and provisioning an SSH key -
# docs/05 "Build-time user & SSH" covers what that would take.
IMAGE_FEATURES += "empty-root-password allow-empty-password allow-root-login"

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

# USB-serial (docs/05 "Local device passthrough into containers") comes from
# `kernel-modules` above, which installs every module the kernel built - and
# CONFIG_USB_SERIAL plus the ftdi_sio/cp210x/ch341/pl2303 drivers are all set
# to =m in recipes-kernel/linux/files/boat-docker.cfg.
#
# This used to also carry MACHINE_ESSENTIAL_EXTRA_RRECOMMENDS +=
# " kernel-module-usb-serial", which did nothing at all: that variable is read
# by packagegroup-core-boot.bb, a DIFFERENT recipe, which cannot see an
# assignment made here - the same per-recipe parse trap this file warns about
# for DISTRO_FEATURES a few lines down. The name was wrong as well. OE splits
# kernel-module packages on the .ko BASENAME, so CONFIG_USB_SERIAL=m yields
# usbserial.ko and therefore `kernel-module-usbserial` - no hyphen. (Compare
# docs/05's `kernel-module-rtc-ds1307` for rtc-ds1307.ko.)
#
# Deliberately NOT re-added under the corrected name: IMAGE_INSTALL is a HARD
# dependency, so a name that is still wrong fails the whole image with
# "Nothing RPROVIDES", and the exact split-package names cannot be confirmed
# without a kernel build. If `kernel-modules` is ever dropped for size, name
# the specific drivers then and verify them with
# `oe-pkgdata-util list-pkgs | grep -i usbserial`:
#   kernel-module-usbserial  kernel-module-ftdi-sio  kernel-module-cp210x
#   kernel-module-ch341      kernel-module-pl2303    kernel-module-cdc-acm
#
# CAN kernel modules dropped: NMEA 2000/CAN is provided by an external
# interface now, not this host (docs/05 "What changed from the earlier
# scaffold").

# Give the rootfs headroom for Docker's local image cache, logs, and compose
# state before /data (docs/05 "Reliability") is provisioned and dockerd's
# data-root actually moves there. Bump if `docker pull` starts failing ENOSPC.
IMAGE_ROOTFS_EXTRA_SPACE = "4194304"

# What this image cannot be built without. DISTRO_FEATURES is evaluated
# per-recipe at parse time against the global distro config, so an
# image-recipe-local append cannot retroactively unskip another recipe that
# already parsed as "missing required distro feature" - which is why these live
# in local.conf (scripts/02-configure-build.sh writes them) and why the
# `DISTRO_FEATURES:append = " systemd"` that used to sit here was not merely
# redundant but actively misleading: it would have made THIS recipe parse as if
# systemd were enabled while every package in the image had been built for
# sysvinit, producing an inconsistent rootfs rather than an error.
#
# features_check turns each of those into a legible parse-time skip naming the
# missing feature, instead of a failure somewhere downstream:
#   systemd        - INIT_MANAGER = "systemd" in local.conf
#   virtualization - docker-ce and nvidia-container-toolkit
#   x11 + opengl   - the Xorg/XFCE helm desktop
#   pam            - the logind session the autologin desktop needs
#   polkit         - reboot/shutdown/suspend from that desktop
inherit features_check
REQUIRED_DISTRO_FEATURES = "systemd virtualization x11 opengl pam polkit"

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
# Empty rather than locked is intentional for this image, and the SSH side of
# it is declared up top with empty-root-password / allow-empty-password /
# allow-root-login. See that block for what the combination means: with
# /etc/sudoers.d/boat also granting passwordless sudo, anyone who can reach
# port 22 has root without a credential.
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

# Let the console/desktop user reach root without depending on the empty
# passwords declared up top. Those make root reachable today, but they are a
# deliberate bench-image choice that may be reversed; this rule is what keeps
# the desktop session able to escalate either way, and is what would make
# removing them survivable.
#
# NOPASSWD is not laziness. It was originally forced - the account's password
# was locked ('*'), so no prompt could ever be satisfied - and it stays now
# that the password is empty instead, because an empty password is no better a
# thing to prompt for. The grant is no wider than
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
# Installed by boat-image.bb. NOPASSWD because the account's password is EMPTY
# (useradd -p '' in that recipe) - prompting for an empty password buys
# nothing, and this grant is no wider than the tty1 autologin already implies.
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
    # Guarded on NetworkManager actually being in the image. Masking both
    # networkd units in an image that then turns out to have no network manager
    # at all leaves it with no networking and no error to explain why - and
    # this recipe cannot see what packagegroup-boat-connectivity installs.
    if [ ! -e ${IMAGE_ROOTFS}${systemd_system_unitdir}/NetworkManager.service ]; then
        bbwarn "NetworkManager is not in this image; leaving systemd-networkd unmasked"
        return
    fi
    install -d ${IMAGE_ROOTFS}${sysconfdir}/systemd/system
    for u in systemd-networkd.service systemd-networkd-wait-online.service; do
        ln -sf /dev/null ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/$u
    done
}
ROOTFS_POSTPROCESS_COMMAND += "mask_unused_networkd; "
