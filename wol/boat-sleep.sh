#!/usr/bin/env bash
# Put the boat computer into SC7 deep sleep, from this machine.
#
#   wol/boat-sleep.sh              # suspend, then confirm it actually went down
#   wol/boat-sleep.sh --status     # report readiness, suspend nothing
#   wol/boat-sleep.sh --force      # suspend even if Wake-on-LAN is not armed
#   wol/boat-sleep.sh --no-wait    # fire and return, don't wait for silence
#
# This is a thin remote wrapper: the decisions all belong to `boat-sleep` on
# the board, which refuses to suspend unless something can wake it again.
# What this adds is the half you cannot get from the far end - watching from
# outside to confirm the board really did go quiet, because `boat-sleep`
# returns as soon as logind accepts the request and its SSH connection then
# dies either way, whether the suspend worked or the box just hung.
#
# Wake it again with: wol/boat-wake.sh
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

WAIT=1
PASSTHRU=()
for arg in "$@"; do
    case "$arg" in
        --no-wait) WAIT=0 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         PASSTHRU+=("$arg") ;;
    esac
done

require_conf BOAT_HOST

# --status must not be followed by a "did it go down" wait: nothing was asked
# to go down.
for arg in "${PASSTHRU[@]:-}"; do
    [[ "$arg" == "--status" || "$arg" == "-s" ]] && WAIT=0
done

log "asking ${BOAT_SSH_USER}@${BOAT_HOST} to suspend ..."
# shellcheck disable=SC2086
if (( WAIT )); then
    # The connection is expected to drop under us on a successful suspend,
    # and ssh reports that as a non-zero exit - so its status is worthless
    # here. The liveness check below is the honest test, and it runs either
    # way.
    ssh -o ConnectTimeout=10 ${BOAT_SSH_OPTS} "${BOAT_SSH_USER}@${BOAT_HOST}" \
        boat-sleep "${PASSTHRU[@]:-}" || true
else
    # Nothing is going to drop the connection (--status) or nothing is going
    # to be verified (--no-wait), so a failed ssh here is just a failure and
    # swallowing it would report success for a command that never ran. This
    # is how a stale host key or a wrong address stays visible.
    ssh -o ConnectTimeout=10 ${BOAT_SSH_OPTS} "${BOAT_SSH_USER}@${BOAT_HOST}" \
        boat-sleep "${PASSTHRU[@]:-}"
fi

if (( WAIT == 0 )); then
    exit 0
fi

log "waiting for the board to go quiet ..."
if wait_for_state down "$BOAT_SLEEP_TIMEOUT"; then
    log "board is asleep. Wake it with: wol/boat-wake.sh"
else
    warn "board still answering after ${BOAT_SLEEP_TIMEOUT}s - it did NOT suspend."
    warn "run 'wol/boat-sleep.sh --status' to see what boat-sleep objects to;"
    warn "the usual answer is that Wake-on-LAN could not be armed, and it"
    warn "refuses to sleep somewhere it cannot be woken from."
    exit 1
fi
