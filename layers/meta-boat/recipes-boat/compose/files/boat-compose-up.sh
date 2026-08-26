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

# --down is boat-compose.service's ExecStop. Without it, `systemctl stop
# boat-compose` only marks the unit inactive and leaves every container
# running, so the stack and systemd's idea of it drift apart.
ACTION=up
case "${1:-}" in
    ''|--up)  ACTION=up ;;
    --down)   ACTION=down ;;
    *)        echo "boat-compose: unknown argument '$1' (usage: boat-compose-up [--up|--down])" >&2
              exit 2 ;;
esac

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "boat-compose: no ${COMPOSE_FILE} yet - seed /data/compose from" \
         "/usr/share/boat/compose-examples/ and re-run 'systemctl start boat-compose'."
    exit 0
fi

if [ "$ACTION" = "down" ]; then
    exec docker compose -f "${COMPOSE_FILE}" down
fi

exec docker compose -f "${COMPOSE_FILE}" up -d
