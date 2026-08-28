#!/usr/bin/env bash
# Put the boat computer to sleep with one signed UDP packet - no SSH.
#
#   wol/boat-sleep-udp.sh              # send it, then watch for the board to go quiet
#   wol/boat-sleep-udp.sh --no-wait    # fire and return
#   wol/boat-sleep-udp.sh --print      # show the packet in hex, send nothing
#
# This is boat-wake.sh's counterpart, and it is deliberately NOT a magic
# packet. Waking is done inside the NIC, which matches a magic packet while the
# host is in SC7 and asserts PME; there is no hardware equivalent for sleeping,
# so this has to be a service on the running board - meta-boat's
# boat-sleep-listener, which runs `boat-sleep` on a valid packet.
#
# And because it IS a service rather than a NIC filter, it has to be
# authenticated. A magic packet carries no secret at all: it is the target MAC
# repeated sixteen times, forgeable by anyone who has seen one frame from the
# board. "Suspend the navigation computer" on those terms means any guest on
# the marina wifi can do it. So every packet here carries an HMAC-SHA256 over a
# timestamp and a nonce, keyed by a secret shared with the board.
#
# The key is the one in /etc/boat-sleep.key on the board, generated per board
# on first boot. Copy it to this machine:
#
#     ssh root@boat cat /etc/boat-sleep.key > wol/boat-sleep.key
#     chmod 0600 wol/boat-sleep.key
#
# wol/boat-sleep.key is git-ignored, like wol/boat.conf, and for the same
# reason only more so.
#
# Compared with wol/boat-sleep.sh, which does this over SSH: no credentials, no
# host key to re-accept after every reflash, and it works when sshd is down.
# What it gives up is boat-sleep's output - the far end's checks still run, but
# their reasons land in the board's journal instead of on your terminal. When
# something is wrong, ask over SSH: `wol/boat-sleep.sh --status`.
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT=1
PRINT_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-wait) WAIT=0 ;;
        --print)   PRINT_ONLY=1; WAIT=0 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         err "unknown argument '$arg'"; exit 1 ;;
    esac
done

require_conf BOAT_HOST

: "${BOAT_SLEEP_PORT:=9099}"
: "${BOAT_SLEEP_KEY_FILE:=${WOL_DIR}/boat-sleep.key}"

# Digits AND range. Out of range is not silent - python's sendto raises, and
# `set -e` stops us - but what the user sees is a five-line traceback ending in
# "OSError: [Errno 22] Invalid argument", which names neither the variable nor
# the file it came from. Every other setting in this repo fails with a sentence
# saying what to fix; this one should too.
case "${BOAT_SLEEP_PORT}" in
    ''|*[!0-9]*) err "BOAT_SLEEP_PORT must be a whole number, got '${BOAT_SLEEP_PORT}'"
                 err "check wol/boat.conf, or the value in your environment"
                 exit 1 ;;
esac
if (( BOAT_SLEEP_PORT < 1 || BOAT_SLEEP_PORT > 65535 )); then
    err "BOAT_SLEEP_PORT must be between 1 and 65535, got '${BOAT_SLEEP_PORT}'"
    err "it must match ListenDatagram= in boat-sleep-listener.socket on the board"
    exit 1
fi

if [[ ! -r "${BOAT_SLEEP_KEY_FILE}" ]]; then
    err "no key at ${BOAT_SLEEP_KEY_FILE}"
    err "copy it from the board, which generated its own on first boot:"
    err "    ssh ${BOAT_SSH_USER}@${BOAT_HOST} cat /etc/boat-sleep.key > ${BOAT_SLEEP_KEY_FILE}"
    err "    chmod 0600 ${BOAT_SLEEP_KEY_FILE}"
    exit 1
fi

# Same check the board makes on its own copy. A shared secret that every local
# account can read is not one, and this is the secret that decides who may
# suspend the boat.
perms="$(stat -c '%a' "${BOAT_SLEEP_KEY_FILE}" 2>/dev/null || stat -f '%Lp' "${BOAT_SLEEP_KEY_FILE}")"
if [[ "${perms}" != "600" && "${perms}" != "400" ]]; then
    err "${BOAT_SLEEP_KEY_FILE} is mode ${perms}; it must not be readable by group or others"
    err "    chmod 0600 ${BOAT_SLEEP_KEY_FILE}"
    exit 1
fi

need python3

# Built in Python for the same reason wake-boat.sh is: the packet is binary,
# the build host already has python3, and hand-rolling HMAC-SHA256 in shell
# around openssl(1) means getting the hex/binary boundary right in three places
# instead of none.
packet_hex="$(python3 - "${BOAT_SLEEP_KEY_FILE}" <<'PY'
import hashlib, hmac, os, struct, sys, time

# Protocol v1; keep in step with boat-sleepd.py, which documents the layout.
MAGIC, VERSION, OP_SLEEP = b"BOATSLP1", 1, 1
FMT = "!8sBBHQQ"

with open(sys.argv[1]) as fh:
    text = fh.read().strip()
try:
    key = bytes.fromhex(text)
except ValueError:
    sys.exit(f"the key in {sys.argv[1]} is not hex - it should be the board's "
             f"/etc/boat-sleep.key verbatim")
if len(key) < 16:
    sys.exit(f"the key in {sys.argv[1]} is only {len(key)} bytes")

header = struct.pack(FMT, MAGIC, VERSION, OP_SLEEP, 0,
                     int(time.time()), int.from_bytes(os.urandom(8), "big"))
print((header + hmac.new(key, header, hashlib.sha256).digest()).hex())
PY
)"

if (( PRINT_ONLY )); then
    log "packet for ${BOAT_HOST}:${BOAT_SLEEP_PORT} (${#packet_hex} hex chars, $(( ${#packet_hex} / 2 )) bytes):"
    printf '%s\n' "${packet_hex}"
    log "not sent (--print)"
    exit 0
fi

log "Asking ${BOAT_HOST}:${BOAT_SLEEP_PORT} to suspend (signed packet)"
python3 - "${BOAT_HOST}" "${BOAT_SLEEP_PORT}" "${packet_hex}" <<'PY'
import socket
import sys

host, port, packet_hex = sys.argv[1], int(sys.argv[2]), sys.argv[3]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(bytes.fromhex(packet_hex), (host, port))
PY

# Nothing comes back, by design: the listener answers no packet at all, valid
# or not, so it cannot be used as a reflector and so a prober learns nothing
# from the difference between "bad signature" and "replayed". Which means the
# ONLY confirmation available is the board going quiet - exactly what
# boat-sleep.sh watches for over SSH, and the reason --no-wait is not the
# default here.
if (( WAIT )); then
    log "waiting up to ${BOAT_SLEEP_TIMEOUT}s for it to go quiet"
    if wait_for_state down "${BOAT_SLEEP_TIMEOUT}"; then
        log "down after ${WAIT_ELAPSED}s"
        log "wake it with: wol/boat-wake.sh"
    else
        err "still answering after ${WAIT_ELAPSED}s"
        err "the packet is unacknowledged, so this means one of:"
        err "  - it never arrived (wrong host/port, or a firewall)"
        err "  - the listener refused it (clock skew, or the wrong key)"
        err "  - boat-sleep refused to suspend - most likely Wake-on-LAN is"
        err "    not armed, which is it doing its job, not failing"
        err "the board's journal has the reason:"
        err "    ssh ${BOAT_SSH_USER}@${BOAT_HOST} journalctl -u boat-sleep-listener -n 20"
        exit 1
    fi
fi
