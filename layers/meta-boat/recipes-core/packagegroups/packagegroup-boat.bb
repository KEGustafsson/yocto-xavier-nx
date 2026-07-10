SUMMARY = "Software for a boat / marine embedded computer"
DESCRIPTION = "Navigation, instrumentation, connectivity and reliability \
packages for an always-on marine computer on the Jetson Xavier NX."
LICENSE = "MIT"

inherit packagegroup

# Split into sub-groups so an image can pick just what it needs.
PACKAGES = "\
    ${PN} \
    ${PN}-nav \
    ${PN}-connectivity \
    ${PN}-canbus \
    ${PN}-runtime \
    ${PN}-reliability \
    ${PN}-tools \
"

RDEPENDS:${PN} = "\
    ${PN}-nav \
    ${PN}-connectivity \
    ${PN}-canbus \
    ${PN}-runtime \
    ${PN}-reliability \
    ${PN}-tools \
"

# --- Navigation / positioning / time -------------------------------------
# gpsd + clients (GNSS), chrony disciplined by GPS/PPS for accurate time.
RDEPENDS:${PN}-nav = "\
    gpsd \
    gpsd-conf \
    gpsd-gpsctl \
    chrony \
"

# --- Connectivity: MQTT telemetry, mDNS discovery, Wi-Fi AP, serial -------
RDEPENDS:${PN}-connectivity = "\
    mosquitto \
    mosquitto-clients \
    avahi-daemon \
    avahi-utils \
    hostapd \
    dnsmasq \
    iw \
    wireless-regdb-static \
    socat \
"

# --- NMEA 2000 / CAN bus --------------------------------------------------
RDEPENDS:${PN}-canbus = "\
    can-utils \
    iproute2 \
"

# --- Application runtime (Signal K server is a Node.js app) ---------------
RDEPENDS:${PN}-runtime = "\
    nodejs \
    nodejs-npm \
    python3 \
    python3-pip \
"

# --- Reliability for an unattended, power-cycled system -------------------
RDEPENDS:${PN}-reliability = "\
    watchdog \
    watchdog-keepalive \
"

# --- Field diagnostics / serviceability -----------------------------------
RDEPENDS:${PN}-tools = "\
    openssh \
    i2c-tools \
    minicom \
    usbutils \
    pciutils \
    nvme-cli \
    htop \
    nano \
    tmux \
    rsync \
"

# NOTE: package names above come from poky + meta-oe/meta-networking/meta-python
# on kirkstone. If bitbake reports "Nothing PROVIDES X", confirm the recipe
# exists on your branch:  bitbake-layers show-recipes '*name*'  and adjust.
