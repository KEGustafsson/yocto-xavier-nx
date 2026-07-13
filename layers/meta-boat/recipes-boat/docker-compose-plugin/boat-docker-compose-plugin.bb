SUMMARY = "Docker Compose v2 CLI plugin ('docker compose ...')"
DESCRIPTION = "This project's fetched meta-virtualization snapshot only \
packages the old Python-based v1 client (python3-docker-compose, invoked \
as the hyphenated 'docker-compose'). This recipe vendors the official \
static v2 binary release so 'docker compose ...' (the space-separated CLI \
plugin form used in current Docker/Compose docs) works too, without \
needing a live device to manually curl it into ~/.docker/cli-plugins \
(confirmed working that way on hardware first - see \
docs/05-phase2-boat-computer-layer.md)."
HOMEPAGE = "https://github.com/docker/compose"
LICENSE = "Apache-2.0"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/Apache-2.0;md5=89aea4e17d99a7cacdbeed46a0096b10"

COMPOSE_PV = "5.3.1"

# Prebuilt static Go binary, arm64 only - this project only ever targets
# aarch64 Jetson machines, so no multi-arch SRC_URI branching.
SRC_URI = "https://github.com/docker/compose/releases/download/v${COMPOSE_PV}/docker-compose-linux-aarch64;downloadfilename=docker-compose-v${COMPOSE_PV}-linux-aarch64;name=compose"
SRC_URI[compose.sha256sum] = "aa611e811d0ea25897839c404bfb5bf93ce706dc51c500a4457890f5d0606a86"

COMPATIBLE_HOST = "aarch64.*-linux"

S = "${WORKDIR}"

# Not built by the OE toolchain - skip checks that only make sense for
# locally-compiled ELF binaries.
INSANE_SKIP:${PN} = "already-stripped ldflags arch"

do_install() {
    # Docker's CLI plugin search path list includes this system-wide
    # directory (matches the official docker-compose-plugin .deb layout).
    install -d ${D}${libexecdir}/docker/cli-plugins
    install -m 0755 ${WORKDIR}/docker-compose-v${COMPOSE_PV}-linux-aarch64 \
        ${D}${libexecdir}/docker/cli-plugins/docker-compose
}

FILES:${PN} = "${libexecdir}/docker/cli-plugins/docker-compose"

RDEPENDS:${PN} = "docker-ce"
