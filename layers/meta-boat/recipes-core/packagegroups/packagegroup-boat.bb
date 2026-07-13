SUMMARY = "Software for a boat / marine embedded computer (container host)"
DESCRIPTION = "Turns the base Xavier NX image into a minimal, reliable Jetson \
container host: Docker + the NVIDIA container runtime, HMI (Weston), \
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

# --- HMI: Weston compositor for a container'd browser as the helm UI -------
# wvkbd (on-screen keyboard) is not packaged in the kirkstone-era layers this
# project fetches - add it from a newer meta-oe snapshot if a touch-only helm
# needs one; omitted here rather than left as a name that fails the build.
# weston-xwayland: the actual xwayland.so compositor module weston.ini's
# xwayland=true loads at startup - it's a SEPARATE package from plain
# `weston` (FILES:${PN}-xwayland in weston_10.0.2.bb), not bundled in by
# default even though weston's own PACKAGECONFIG built it once x11+wayland
# DISTRO_FEATURES are both present. CONFIRMED ON HARDWARE: without this
# package, weston fatally fails to load xwayland.so ("No such file or
# directory") and crash-loops the whole session (repeated login-screen
# flicker) - do not drop this. weston-xwayland itself RDEPENDS on
# `xwayland` (the X server binary), so that's not listed separately here.
# xauth/xhost: boat-hmi-autostart grants local X11 access via xhost once
# XWayland's socket appears - see docs/05 "Container GUI apps on the HDMI
# screen (X11 via XWayland)".
# libinput/fontconfig deliberately NOT listed here: weston DEPENDS on both
# (directly, and transitively via pango) so its own package already
# RDEPENDS on the correctly shlib-renamed runtime packages. An allarch
# packagegroup listing the plain names itself breaks once x11 support makes
# them dynamically renamed (do_package_write_rpm QA error) - let weston
# pull them in.
RDEPENDS:${PN}-hmi = "\
    weston \
    weston-xwayland \
    weston-init \
    wayland \
    wayland-protocols \
    ttf-dejavu-sans \
    xauth \
    xhost \
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
