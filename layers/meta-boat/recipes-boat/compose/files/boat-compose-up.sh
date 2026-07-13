#!/bin/sh
# ExecStart for boat-compose.service. Compose files are hand-managed config-
# as-code on /data (docs/05 "Compose as config-as-code (git)"), not baked
# into the image, so this only acts if the operator has seeded /data/compose.
#
# Uses the v2 `docker compose` (space-separated) plugin - boat-docker-
# compose-plugin, vendored specifically for this. CONFIRMED ON HARDWARE:
# the v1 `docker-compose` (hyphenated) CLI this used to call
# (python3-docker-compose, the only compose client this project's
# kirkstone-era meta-virtualization packages) fails on this image with
# "ModuleNotFoundError: No module named 'distutils'" - python3 here is
# 3.10.20 (still ships distutils upstream), so this is a missing
# python3-distutils RDEPENDS on packagegroup-boat, not a Python version
# mismatch; fix that properly and this could switch back, but v2 works
# today without needing that fix.
set -eu

COMPOSE_DIR=/data/compose
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "boat-compose: no ${COMPOSE_FILE} yet - seed /data/compose from" \
         "/usr/share/boat/compose-examples/ and re-run 'systemctl start boat-compose'."
    exit 0
fi

exec docker compose -f "${COMPOSE_FILE}" up -d
