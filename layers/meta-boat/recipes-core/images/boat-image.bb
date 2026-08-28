SUMMARY = "Boat / marine computer image for Jetson Xavier NX (NVMe boot)"
LICENSE = "MIT"

# Start from a console-only base with the standard command-line tooling.
require recipes-core/images/core-image-base.bb

IMAGE_FEATURES += "ssh-server-openssh"

# root and "boat" share a real password, set from BOAT_PASSWORD_HASH below.
#
# This replaces the EMPTY passwords the image used to ship with, where anyone
# who could reach port 22 got root with no credential at all. Two of the three
# IMAGE_FEATURES that made that work are therefore gone:
#
#   empty-root-password   REMOVED - it suppressed the zap_empty_root_password
#                         rootfs postprocess so root's shadow field stayed
#                         empty. With a hash set explicitly below, letting that
#                         postprocess run is harmless: it only zaps an EMPTY
#                         field, and ours is not.
#   allow-empty-password  REMOVED - sshd PermitEmptyPasswords. Nothing has an
#                         empty password now, and leaving it would keep the
#                         door open for anything that later grew one.
#   allow-root-login      KEPT - sshd PermitRootLogin yes. Without it sshd
#                         falls back to prohibit-password and root cannot log
#                         in over the network AT ALL, password or not, which
#                         would break `ssh root@boat` entirely.
#
# WHAT THIS IS AND IS NOT
# It is a real credential where there was none. It is NOT a secret: the hash
# is in this file, in a public repository, and is the same on every board built
# from it. Treat it as "keeps a passer-by out", not "keeps an attacker out".
# For anything deployed rather than benched, provision an SSH key and lock the
# passwords - docs/05 "Build-time user & SSH" covers what that takes.
#
# Note also that "boat" keeps passwordless sudo and docker-group membership
# (both root-equivalent), so a shell as "boat" is still a shell as root without
# re-entering anything. The password raises the floor; it does not partition
# the system.
#
# To change it, generate a new SHA-512 crypt hash and set BOAT_PASSWORD_HASH in
# local.conf - no need to edit this recipe:
#
#     openssl passwd -6 'your-password'
#
# -p takes an ENCRYPTED password, never plaintext. kirkstone's shadow has no
# clear-text option here, and a plaintext string in -p would be written to
# /etc/shadow verbatim and match nothing, locking the account while looking
# like it worked.
BOAT_PASSWORD_HASH ?= "$6$/KkGOgbdaegY3Cmb$Xc81TWWLfcbz8hRvN.dENbr0Eg11V3Js2Wk.jsrvcsCznrMPbrUaMk49/4Pgx8HIwq5QZl4uf.mjATq.uoDO40"

IMAGE_FEATURES += "allow-root-login"

IMAGE_INSTALL:append = " \
    packagegroup-boat \
    boat-docker-config \
    boat-hmi-autostart \
    boat-compose \
    boat-power \
    boat-sleep-listener \
    boat-grow-rootfs \
    boat-firefox \
    kernel-modules \
    "

# boat-sleep-listener opens ONE UDP port (9099 by default) on which a packet
# signed with this board's own /etc/boat-sleep.key - generated per board on
# first boot, never baked into the image - runs `boat-sleep`. That is a
# deliberate widening of the network surface, and it is here because the
# alternative for "suspend from ashore" is an SSH session whose host key
# changes on every reflash. An unsigned packet, a replayed one, or one outside
# a 30s clock window is dropped without a reply; boat-sleep's own refusal to
# suspend a board that nothing can wake still applies on top.
#
# Drop this one line to remove the port entirely; wol/boat-sleep.sh (over SSH)
# keeps working either way.

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
# -p '${BOAT_PASSWORD_HASH}' gives "boat" a real password rather than the empty
# one this used to be (and, before that, a locked '*' that no string could ever
# satisfy, leaving the account reachable only through the tty1 autologin).
# root gets the same hash from the usermod below.
#
# See the BOAT_PASSWORD_HASH block up top for what this does and does not buy.
# Short version: it is a credential where there was none, it is not a secret,
# and /etc/sudoers.d/boat still grants passwordless sudo - so a "boat" shell is
# a root shell without re-entering anything.
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
    useradd -u 2000 -m -s /bin/bash -p '${BOAT_PASSWORD_HASH}' boat; \
    usermod -a -G video,render,input,dialout,i2c,spi,docker,jtop boat; \
    usermod -s /bin/bash root; \
    usermod -p '${BOAT_PASSWORD_HASH}' root; \
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
# NOPASSWD is not laziness, though it is now the weakest link. It was
# originally forced - the account's password was locked ('*'), so no prompt
# could ever be satisfied - and it survived the move to an empty password,
# where prompting bought nothing. Now that "boat" has a REAL password, this is
# the one place still worth revisiting: dropping NOPASSWD would make sudo
# actually ask. It is kept for now because the grant is no wider than
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
# Installed by boat-image.bb. NOPASSWD because this grant is no wider than the
# tty1 autologin already implies: that session is "boat", and "boat" is in the
# docker group, which is root-equivalent. Note the account DOES have a real
# password now - so unlike before, making this prompt would achieve something.
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
