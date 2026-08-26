#!/usr/bin/env bash
# Wake the boat computer from SC7 with a Wake-on-LAN magic packet, and wait
# until it actually answers.
#
#   wol/boat-wake.sh              # send, then wait for the board to come back
#   wol/boat-wake.sh --no-wait    # send and return immediately
#
# The packet itself is built and sent by scripts/wake-boat.sh - deliberately
# not reimplemented here, so there is one sender to keep correct. What this
# adds is the waiting, and telling you whether it worked: nothing
# acknowledges a magic packet, so "sent" and "woke up" are different claims
# and only the second one is worth anything.
#
# Put it back to sleep with: wol/boat-sleep.sh
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT=1
for arg in "$@"; do
    case "$arg" in
        --no-wait) WAIT=0 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         die "unknown argument '$arg'" ;;
    esac
done

require_conf BOAT_MAC

if (( WAIT )) && [[ -n "$BOAT_HOST" ]] && boat_is_up; then
    log "board at ${BOAT_HOST} is already awake - nothing to do"
    exit 0
fi

"${REPO_ROOT}/scripts/wake-boat.sh" "$BOAT_MAC" "$BOAT_BROADCAST"

if (( WAIT == 0 )); then
    exit 0
fi

if [[ -z "$BOAT_HOST" ]]; then
    warn "BOAT_HOST not set - packet sent, but there is no address to watch."
    exit 0
fi

log "waiting for ${BOAT_HOST} to answer ..."
if wait_for_state up "$BOAT_WAKE_TIMEOUT"; then
    log "board is awake."
    log "ssh ${BOAT_SSH_USER}@${BOAT_HOST}"
else
    err "no answer after ${WAIT_ELAPSED}s."
    err "Things to check, in the order they actually go wrong:"
    err "  1. was Wake-on-LAN armed BEFORE it slept?  ethtool eth0 | grep Wake-on"
    err "     (it must read 'g', not 'd' - and it has to be that way at the"
    err "      moment of suspend, not just at boot)"
    err "  2. is this host on the same layer-2 segment as the boat? a directed"
    err "     broadcast (BOAT_BROADCAST=192.168.0.255) only crosses subnets if"
    err "     the router forwards it, and most do not"
    err "  3. did it suspend at all, or power off? a board that is off does not"
    err "     answer magic packets"
    exit 1
fi
