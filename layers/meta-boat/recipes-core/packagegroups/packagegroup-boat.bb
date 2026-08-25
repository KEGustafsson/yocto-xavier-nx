SUMMARY = "Software for a boat / marine embedded computer (container host)"
DESCRIPTION = "Turns the base Xavier NX image into a minimal, reliable Jetson \
container host: Docker + the NVIDIA container runtime, HMI (XFCE on Xorg), \
connectivity and reliability tooling. Applications (Signal K, DeepStream, \
Firefox, ...) run as containers - see docs/05-phase2-boat-computer-layer.md."
LICENSE = "MIT"

inherit packagegroup

# Split into sub-groups so an image can pick just what it needs.
PACKAGES = "\
    ${PN} \
    ${PN}-containers \
    ${PN}-nvidia-container \
    ${PN}-nvidia-host \
    ${PN}-cuda \
    ${PN}-jetson \
    ${PN}-connectivity \
    ${PN}-hmi \
    ${PN}-reliability \
    ${PN}-security \
    ${PN}-nettools \
    ${PN}-tools \
"

RDEPENDS:${PN} = "\
    ${PN}-containers \
    ${PN}-nvidia-container \
    ${PN}-nvidia-host \
    ${PN}-cuda \
    ${PN}-jetson \
    ${PN}-connectivity \
    ${PN}-hmi \
    ${PN}-reliability \
    ${PN}-security \
    ${PN}-nettools \
    ${PN}-tools \
"

# --- Container runtime -----------------------------------------------------
# docker-ce is meta-virtualization's PREFERRED_PROVIDER_virtual/docker
# default (the alternative, docker-moby, is a valid RPROVIDES "docker" too,
# but gets skipped as a runtime target unless you override that preference).
# containerd-opencontainers/runc-opencontainers come in transitively via its
# virtual/containerd + virtual/runc RDEPENDS. Two docker-compose CLIs ship:
# python3-docker-compose (the only one meta-virtualization packages on this
# kirkstone snapshot - v1, hyphenated "docker-compose up -d") and
# boat-docker-compose-plugin (a locally vendored static v2 binary, giving
# the "docker compose up -d" space-separated form most current docs use -
# confirmed working on hardware).
RDEPENDS:${PN}-containers = "\
    docker-ce \
    python3-docker-compose \
    boat-docker-compose-plugin \
    ca-certificates \
"

# --- NVIDIA container runtime (GPU/DLA/NVENC/NVDEC/ISP in containers) ------
# nvidia-container-toolkit (meta-tegra's external/virtualization-layer, auto-
# included once meta-virtualization is in bblayers.conf) pulls in
# libnvidia-container-tools and tegra-configs-container-csv itself.
# REQUIRED_DISTRO_FEATURES = "virtualization" - must be in DISTRO_FEATURES.
# OPEN RISK (see docs/05): unproven on this kirkstone/L4T combination -
# prototype a `docker run --runtime nvidia ...` GPU test before relying on it.
RDEPENDS:${PN}-nvidia-container = "\
    nvidia-container-toolkit \
"

# --- Tegra userspace driver bits the container runtime bind-mounts in ------
# tegra-libraries-* are already pulled in by the BSP (MACHINE_ESSENTIAL_*
# RDEPENDS in meta-tegra's machine .inc) - not listed again here.
RDEPENDS:${PN}-nvidia-host = "\
    tegra-argus-daemon \
"

# --- CUDA / cuDNN / TensorRT on the host ------------------------------------
# JetPack splits in two: the "Jetson Linux" BSP half (kernel + GPU driver,
# bootloader, Tegra userspace driver libs, nvpmodel, argus) which meta-tegra
# gives us unconditionally, and the "Jetson SDK Components" half - this
# group. Without it the host has only tegra-libraries-cuda, i.e. the CUDA
# *driver* API (libcuda.so, nvvm, ptxjitcompiler) that container CUDA
# runtimes bind against, and nothing that can compile or run a CUDA program
# on the host itself.
#
# Versions are not chosen here - they are whatever this L4T release pins.
# R35.6.4 / JetPack 5.1.6 means CUDA 11.4 (CUDA_VERSION in meta-tegra's
# conf/machine/include/tegra-common.inc), cuDNN 8.6.0, TensorRT 8.5.2.
#
# All of these come from NVIDIA's *public* Jetson deb feed via meta-tegra's
# l4t_deb_pkgfeed class (repo.download.nvidia.com/jetson) - no developer
# login or manual download step. They are proprietary, which is already
# covered by the LICENSE_FLAGS_ACCEPTED += "commercial" that
# scripts/02-configure-build.sh writes into local.conf.
#
# cuda-toolkit is the full on-device SDK (nvcc, headers, static libs) - the
# same thing sdkmanager installs on a JetPack device, and what you want if
# anything is ever built or profiled on the board. For a runtime-only host
# swap it for `cuda-libraries` (cudart, nvrtc, cublas, cufft, curand,
# cusolver, cusparse, npp, cudla) and drop several GB.
#
# tensorrt-plugins / tensorrt-trtexec are named as the virtual PROVIDES
# rather than the concrete -prebuilt recipes on purpose: tegra-common.inc
# already sets PREFERRED_PROVIDER for both to the prebuilt NVIDIA debs, which
# avoids a long from-source cmake build of NVIDIA/TensorRT.git. Naming the
# virtual leaves that choice with the BSP instead of pinning it here.
# python3-tensorrt is the one recipe in this group that IS built from source.
#
# SIZE: expect this group to add several GB to the rootfs (cuDNN and TensorRT
# ship static archives alongside the shared libs) - it is most of the reason
# boat-image's content sits around 5 GiB. The flashed rootfs is fixed at
# ROOTFS_SIZE_BYTES (16 GiB, scripts/env.sh), which leaves comfortable room;
# boat-grow-rootfs claims the rest of the SSD on first boot.
#
# Unlike the `dbus` case noted under -hmi, naming these from an allarch
# packagegroup is safe: debian.bbclass only renames a package holding exactly
# one shared library, and cuda-toolkit is an ALLOW_EMPTY metapackage while
# cudnn/tensorrt-core/tensorrt-plugins each ship two or more.
# OpenCV, and it comes out BETTER here than a JetPack install gives you:
# meta-tegra's opencv_4.5.%.bbappend turns on -DWITH_CUDA=ON with
# CUDA_ARCH_BIN="7.2" (TEGRA_CUDA_ARCHITECTURE=72 for tegra194) and adds
# "cuda dnn" to PACKAGECONFIG automatically, because "cuda" is in
# MACHINEOVERRIDES for every Tegra machine. It also pulls NVIDIA's Optical
# Flow SDK in. JetPack's own libopencv debs are built WITHOUT CUDA - the
# stock sdkmanager OpenCV cannot use the GPU at all - so this is the
# accelerated build, including the CUDA-backed dnn module.
#
# That does mean OpenCV here hard-depends on the CUDA toolkit and cuDNN
# above; it is in this sub-group rather than -tools for exactly that reason.
#
# VPI 2 (libnvvpi2) is the remaining JetPack SDK component with real runtime
# content: NVIDIA's Vision Programming Interface, which is the only way to
# reach the PVA and VIC engines from application code - CUDA and TensorRT
# cannot. Xavier NX has two PVA cores that otherwise sit idle.
#
# tegra-mmapi is the Multimedia API, and it needs care: its main package is
# ALLOW_EMPTY and installs NOTHING - the recipe only ships headers, all of
# which land in tegra-mmapi-dev. The mmapi *runtime* (tegra-libraries-camera,
# -multimedia, -multimedia-utils) is already in the image via the BSP's
# MACHINE_ESSENTIAL deps, so naming plain `tegra-mmapi` here would add a
# literal zero bytes. The -dev package is the thing worth having, and only
# because this image carries the full on-device SDK (nvcc, CUDA headers)
# already - headers with no compiler would be pointless.
#
# `opencv` is a metapackage whose RDEPENDS is generated from every runtime
# sub-package (libopencv-core, -imgproc, -dnn, -videoio, ...), so naming it
# gets the whole library set. It also drags in opencv-samples, since the
# default PACKAGECONFIG has "samples" - drop that with a PACKAGECONFIG:remove
# bbappend if the rootfs gets tight. python3-opencv is the cv2 module.
RDEPENDS:${PN}-cuda = "\
    cuda-toolkit \
    cudnn \
    tensorrt-core \
    tensorrt-plugins \
    tensorrt-trtexec \
    python3-tensorrt \
    opencv \
    python3-opencv \
    libnvvpi2 \
    tegra-mmapi-dev \
"

# --- Jetson power/thermal/clocks tools --------------------------------------
RDEPENDS:${PN}-jetson = "\
    tegra-nvpmodel \
    tegra-nvfancontrol \
    tegra-tools \
    python3-jetson-stats \
"

# --- Connectivity: Wi-Fi/cellular/Ethernet failover, mDNS, VPN, MQTT-less --
# (Signal K's own MQTT bridge, if used, runs in its container - mosquitto is
# no longer a host package now that apps are containerized, see docs/05.)
# networkmanager-nmtui is a SEPARATE package from networkmanager (the recipe
# builds nmtui unconditionally but splits it out), and without it the only
# way to join a Wi-Fi network from a console is hand-written `nmcli`
# invocations. On a boat that is the difference between a five-second job at
# the chart table and a fight. nmcli comes from networkmanager-nmcli, which
# networkmanager's own PACKAGECONFIG already pulls in.
# The Wi-Fi/Bluetooth radios themselves need nothing listed here: the Xavier
# NX devkit's on-board RTL8822CE is handled by meta-tegra's tegra-wifi /
# tegra-bluetooth / tegra-firmware-rtl8822 (MACHINE_ESSENTIAL) plus the
# kernel's rtl8822ce+btrtl modules from `kernel-modules`, and poky's default
# DISTRO_FEATURES already carries "wifi bluetooth", which is what switches on
# networkmanager's wifi and bluez5 PACKAGECONFIGs.
RDEPENDS:${PN}-connectivity = "\
    networkmanager \
    networkmanager-nmtui \
    modemmanager \
    avahi-daemon \
    avahi-utils \
    bluez5 \
    hostapd \
    dnsmasq \
    iw \
    wireless-regdb-static \
    wireguard-tools \
    chrony \
"

# --- HMI: XFCE desktop on the HDMI screen ----------------------------------
# The helm display is a full XFCE desktop on Xorg (this replaced the earlier
# Weston/Wayland-only session - see docs/05 "HMI / XFCE autostart"). Marine
# applications themselves still run as containers; the desktop is what shows
# them, plus a file manager/terminal/settings for field work.
#
# packagegroup-core-x11-xserver (poky) rather than a hand-written list of X
# packages: it expands the machine's own XSERVER variable, which meta-tegra
# sets in conf/machine/include/tegra-common.inc to
# "xserver-xorg xf86-input-evdev xserver-xorg-video-nvidia
# xserver-xorg-module-libwfb" - i.e. NVIDIA's Tegra X driver, not the generic
# modesetting one. Hardcoding those names here would silently drift from the
# BSP. It pulls in tegra-configs-xorg (L4T's /etc/X11/xorg.conf for t194) via
# xserver-xorg-video-nvidia's own RDEPENDS.
#
# NOT packagegroup-core-x11 (the superset): its -utils half drags in
# xserver-nodm-init, which ships a display-manager.service alias that would
# start a second, root-owned Xorg racing boat-hmi-autostart's session for the
# same VT. The handful of X utilities actually wanted are listed individually
# below instead.
#
# packagegroup-xfce-base = xfwm4, xfce4-session, xfconf, xfdesktop,
# xfce4-panel + its standard plugins, xfce4-settings, xfce4-notifyd,
# xfce4-terminal, thunar, thunar-volman. It needs meta-xfce in
# bblayers.conf, which drags in meta-gnome and meta-multimedia as layer
# dependencies (scripts/02-configure-build.sh adds all three).
# packagegroup-xfce-extended is deliberately not used: it RRECOMMENDS ~40
# more panel plugins, themes and desktop apps that just grow the rootfs on
# an appliance.
#
# xauth: boat-xfce-session uses it to export a copy of the session's
# MIT-MAGIC-COOKIE for containerized GUI apps, which present it over the
# mounted /tmp/.X11-unix socket - see docs/05 "Container GUI apps on the HDMI
# screen (X11)". `xhost` is deliberately NOT installed: the blanket
# `xhost +local:` grant this image used to rely on disables cookie auth for
# every local uid, and leaving the tool out keeps it from creeping back in.
# dbus/libinput/fontconfig deliberately NOT listed here, even though the
# session needs all three: they are pulled in transitively (xfce4-session
# RDEPENDS "dbus-x11", which poky's dbus package RPROVIDES and which is also
# where boat-xfce-session's dbus-run-session lives; xserver-xorg ->
# xf86-input-libinput; gtk+3 -> fontconfig) under their correctly renamed
# package names. CONFIRMED AT BUILD TIME: naming `dbus` here directly fails
# do_package_write_rpm with "An allarch packagegroup shouldn't depend on
# packages which are dynamically renamed (dbus to dbus-1)" - dbus sets
# DEBIANNAME:${PN} = "dbus-1", and this packagegroup is allarch, so it has
# no arch-specific pkgdata to resolve the rename against. The same trap
# applies to libinput/fontconfig once x11 support makes them shlib-renamed.
# boat-hmi-autostart, which is not allarch, RDEPENDS on `dbus` by name for
# the same binary - that one resolves fine.
#
# network-manager-applet (nm-applet) and blueman are the Wi-Fi and Bluetooth
# front ends for this desktop. The radios, drivers, firmware and daemons are
# all in -connectivity and the BSP already; these two are what makes them
# reachable without a terminal. Both drop an /etc/xdg/autostart entry, so
# xfce4-session starts them and they appear in the panel's systray plugin -
# no wiring needed in boat-hmi-autostart.
#
# Neither needs polkit, which is deliberately not in this image's
# DISTRO_FEATURES: NetworkManager is therefore built with -Dpolkit=false, so
# it does no per-action authorization and the desktop user can manage
# connections directly - the right trade for a single-operator appliance.
# blueman's optional pulseaudio integration is likewise off (no "pulseaudio"
# distro feature), so it brings in no audio stack; its thunar-sendto
# integration IS on, and thunar is already here via packagegroup-xfce-base.
RDEPENDS:${PN}-hmi = "\
    packagegroup-core-x11-xserver \
    packagegroup-xfce-base \
    xinit \
    xauth \
    xrandr \
    xset \
    xdpyinfo \
    ttf-dejavu-sans \
    network-manager-applet \
    blueman \
"

# --- Reliability for an unattended, power-cycled system ---------------------
# `watchdog` (full monitoring daemon) and `watchdog-keepalive` (bare
# pet-the-watchdog-only daemon) are upstream-declared mutually exclusive
# alternatives (RCONFLICTS in watchdog_5.16.bb), not complementary - pick
# one. `watchdog` is the right choice here: it can run custom health checks,
# not just keep the hardware watchdog fed.
# RAUC (A/B updates) needs the meta-rauc layer, not yet fetched by this
# project - see docs/05 "Next steps" #7; add it as a deliberate follow-up.
# fake-hwclock is likewise not packaged in these kirkstone-era layers; fit a
# hardware RTC (docs/05) or backport the recipe if the clock-at-boot gap
# (1970 until the first NTP sync) matters before that lands.
RDEPENDS:${PN}-reliability = "\
    watchdog \
"

# --- Field diagnostics / serviceability -------------------------------------
# sudo: without it the image has NO route from the autologin session to root.
# The "boat" user is created with a locked password ('*' in boat-image.bb),
# and root's password is only empty while IMAGE_FEATURES carries
# "debug-tweaks" - turn that off for a real deployment (which you should: it
# also makes sshd accept empty passwords and root logins) and `su` stops
# working too, leaving the console session with no way to escalate at all.
# boat-image.bb installs the matching /etc/sudoers.d/boat rule; sudo without
# that rule would be equally useless.
RDEPENDS:${PN}-security = "\
    openssh \
    nftables \
    sudo \
"

# --- Network diagnostics (moved out of -connectivity per docs/05) ----------
# bind-utils (dig/nslookup) and wavemon are not packaged in these kirkstone-
# era layers - omitted rather than left as unresolved names.
RDEPENDS:${PN}-nettools = "\
    iproute2 \
    net-tools \
    iputils \
    bmon \
    tcpdump \
    mtr \
    traceroute \
    ethtool \
    iftop \
    curl \
    nmap \
    libqmi \
    libmbim \
"

# Storage/partition tooling is here rather than only as boat-grow-rootfs's
# own RDEPENDS because the same tools are what you reach for by hand when a
# drive misbehaves in the field. e2fsprogs-resize2fs in particular is NOT
# pulled in by e2fsprogs itself (that gives e2fsck/mke2fs/dumpe2fs/badblocks
# only), so without it named somewhere the image has no way to resize an
# ext4 at all. parted is the human-friendly front end; gptfdisk (sgdisk) is
# what boat-grow-rootfs scripts against.
RDEPENDS:${PN}-tools = "\
    nvme-cli \
    parted \
    gptfdisk \
    e2fsprogs-resize2fs \
    i2c-tools \
    usbutils \
    pciutils \
    minicom \
    htop \
    nano \
    tmux \
    rsync \
    git \
    iperf3 \
    bash \
"

# NOTE: package names above were cross-checked against the actual recipes in
# this project's fetched layers (poky, meta-openembedded, meta-tegra,
# meta-virtualization, meta-tegra-community) on kirkstone, not guessed - but
# `kirkstone` branches move. If bitbake reports "Nothing PROVIDES X", confirm
# with `bitbake-layers show-recipes '*name*'` and adjust.
