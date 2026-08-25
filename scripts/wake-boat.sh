#!/usr/bin/env bash
# Send a Wake-on-LAN magic packet to the boat computer, waking it from SC7.
#
#   ./scripts/wake-boat.sh 48:b0:2d:11:22:33
#   BOAT_MAC=48:b0:2d:11:22:33 ./scripts/wake-boat.sh
#   ./scripts/wake-boat.sh 48:b0:2d:11:22:33 192.168.1.255   # directed bcast
#
# The target side is what makes this work at all - meta-boat's boat-power
# recipe arms magic-packet wake at boot and around every suspend, and
# `boat-sleep` on the board refuses to suspend unless it is armed. This
# script is only the sender, and needs nothing installed: the packet is 102
# bytes of UDP built in Python, which the build host already has.
#
# The MAC to use is the one `boat-sleep` prints ("wake it with: wakeonlan
# ..."), or `cat /sys/class/net/eth0/address` on the board.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

MAC="${1:-${BOAT_MAC:-}}"
# 255.255.255.255 reaches the board only from the SAME layer-2 segment: it is
# never routed. From another subnet, pass that subnet's directed broadcast
# address instead (e.g. 192.168.1.255) AND have the router forward directed
# broadcasts - most don't by default, which is the usual reason a magic
# packet "does nothing". Over the internet, wake through a VPN endpoint on
# the boat's LAN (wireguard-tools is on the image for exactly this) rather
# than by port-forwarding UDP 9.
BCAST="${2:-${BOAT_WOL_BROADCAST:-255.255.255.255}}"
PORT="${BOAT_WOL_PORT:-9}"
COUNT="${BOAT_WOL_COUNT:-3}"

if [[ -z "${MAC}" ]]; then
  err "no MAC address given"
  err "usage: $0 <mac> [broadcast-address]   (or set BOAT_MAC)"
  exit 1
fi

need python3

log "Waking ${MAC} via ${BCAST}:${PORT} (${COUNT} packet(s))"

# Repeat by default: a magic packet is a single unacknowledged UDP datagram,
# and a switch that has aged out the port, or a NIC still settling after
# power-on, silently drops it. Three costs nothing.
python3 - "${MAC}" "${BCAST}" "${PORT}" "${COUNT}" <<'PY'
import re
import socket
import sys
import time

mac, bcast = sys.argv[1], sys.argv[2]

hexmac = re.sub(r"[:.\-]", "", mac).lower()
if not re.fullmatch(r"[0-9a-f]{12}", hexmac):
    sys.exit(f"'{mac}' is not a MAC address (expected 6 hex octets, e.g. 48:b0:2d:11:22:33)")

# Validate before opening the socket: a count of 0 (or a negative one) would
# make the send loop a no-op while the script still exited 0 and reported
# "Magic packet sent." - the one outcome this script must never fake, since
# nothing else in the wake path acknowledges anything.
try:
    port, count = int(sys.argv[3]), int(sys.argv[4])
except ValueError:
    sys.exit(f"BOAT_WOL_PORT ('{sys.argv[3]}') and BOAT_WOL_COUNT ('{sys.argv[4]}') must be whole numbers")
if count < 1:
    sys.exit(f"BOAT_WOL_COUNT must be at least 1, got {count}")
if not 0 < port < 65536:
    sys.exit(f"BOAT_WOL_PORT must be between 1 and 65535, got {port}")

# Magic packet: 6 x 0xFF, then the target MAC repeated 16 times.
payload = b"\xff" * 6 + bytes.fromhex(hexmac) * 16

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
try:
    for i in range(count):
        sock.sendto(payload, (bcast, port))
        if i + 1 < count:
            time.sleep(0.2)
finally:
    sock.close()
PY

log "Magic packet sent."
log "The board takes a few seconds to resume; watch for it with:"
log "    ping <boat-ip>        # or: ssh root@<boat> journalctl -b -u boat-wol"
warn "Nothing acknowledges a magic packet - if the board stays asleep, check"
warn "that 'boat-sleep --status' reported Wake-on-LAN armed BEFORE it slept,"
warn "and that this host is on the same layer-2 segment as the boat computer."
