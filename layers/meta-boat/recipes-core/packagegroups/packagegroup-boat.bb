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
RDEPENDS:${PN}-connectivity = "\
    networkmanager \
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
RDEPENDS:${PN}-hmi = "\
    packagegroup-core-x11-xserver \
    packagegroup-xfce-base \
    xinit \
    xauth \
    xrandr \
    xset \
    xdpyinfo \
    ttf-dejavu-sans \
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
RDEPENDS:${PN}-security = "\
    openssh \
    nftables \
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

RDEPENDS:${PN}-tools = "\
    nvme-cli \
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
