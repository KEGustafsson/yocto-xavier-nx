#!/usr/bin/env bash
# Put the boat computer into SC7 deep sleep, from this machine.
#
#   wol/boat-sleep.sh              # suspend, then confirm it actually went down
#   wol/boat-sleep.sh --status     # report readiness, suspend nothing
#   wol/boat-sleep.sh --dry-run    # run every check, stop before suspending
#   wol/boat-sleep.sh --force      # suspend even if Wake-on-LAN is not armed
#   wol/boat-sleep.sh --delay 10   # seconds before logind is asked
#   wol/boat-sleep.sh --no-wait    # fire and return, don't wait for silence
#
# Everything except --no-wait is passed straight through to `boat-sleep` on
# the board; --no-wait is local, and only decides whether to watch.
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
        -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         PASSTHRU+=("$arg") ;;
    esac
done

require_conf BOAT_HOST

# Flags that ask the far end NOT to suspend must not be followed by a "did it
# go down" wait - nothing was asked to go down, so the wait would burn its
# whole timeout and then report a suspend failure that never happened.
# `boat-sleep --status` and `--dry-run` are both explicitly no-suspend modes
# (see the target script's usage), and this has to be decided BEFORE the
# liveness gate below: --status is precisely what you reach for on a board
# that is not answering, and gating it on a successful ping made it unusable
# on exactly that board.
for arg in ${PASSTHRU[@]+"${PASSTHRU[@]}"}; do
    case "$arg" in
        -s|--status|-n|--dry-run) WAIT=0 ;;
    esac
done

# Refuse to report a suspend we never observed. wait_for_state down succeeds
# on its first probe if the board was already unreachable, so without this an
# unplugged or already-sleeping board reads as a successful sleep - and the
# ssh failure below cannot contradict it, because a dropped connection is the
# expected outcome of a real suspend. Only applies when a suspend was actually
# requested, hence the WAIT test.
if (( WAIT )) && ! boat_is_up; then
    die "board at ${BOAT_HOST} is not answering - refusing to claim it slept.
      If it is already asleep, wake it with wol/boat-wake.sh first;
      if it should be up, check the address in wol/boat.conf.
      To ask it what it objects to without suspending anything, add --status."
fi

# boat-sleep needs root. boat.conf.example offers the "boat" user as an
# alternative to root, which only works because of its passwordless sudo - so
# prefix the command for any user that is not root, rather than letting that
# documented configuration fail.
if [[ "$BOAT_SSH_USER" == "root" ]]; then
    REMOTE_CMD=(boat-sleep)
else
    REMOTE_CMD=(sudo -n boat-sleep)
fi

# ssh flattens its command arguments into ONE string and the login shell on
# the board re-splits it, so anything with whitespace or a shell metacharacter
# has to be quoted for that far shell rather than just for this one.
REMOTE_LINE="$(printf '%q ' "${REMOTE_CMD[@]}" ${PASSTHRU[@]+"${PASSTHRU[@]}"})"

# BatchMode=yes as well as ConnectTimeout: ConnectTimeout bounds the TCP
# connect only, so without it ssh sits forever on a host-key or password
# prompt instead of failing. That is not hypothetical here - boat.conf.example
# calls out reflashing, which changes the board's host key every image. It
# comes BEFORE ${BOAT_SSH_OPTS} so an operator can still override it.
SSH_BASE=(ssh -o BatchMode=yes -o ConnectTimeout=10)

log "asking ${BOAT_SSH_USER}@${BOAT_HOST} to suspend ..."
# shellcheck disable=SC2086
if (( WAIT )); then
    # The connection is expected to drop under us on a successful suspend,
    # and ssh reports that as a non-zero exit - so its status is worthless
    # here. The liveness check below is the honest test, and it runs either
    # way.
    "${SSH_BASE[@]}" ${BOAT_SSH_OPTS} "${BOAT_SSH_USER}@${BOAT_HOST}" \
        "${REMOTE_LINE}" || true
else
    # Nothing is going to drop the connection (--status/--dry-run) or nothing
    # is going to be verified (--no-wait), so a failed ssh here is just a
    # failure and swallowing it would report success for a command that never
    # ran. This is how a stale host key or a wrong address stays visible.
    "${SSH_BASE[@]}" ${BOAT_SSH_OPTS} "${BOAT_SSH_USER}@${BOAT_HOST}" \
        "${REMOTE_LINE}"
fi

if (( WAIT == 0 )); then
    exit 0
fi

log "waiting for the board to go quiet ..."
if wait_for_state down "$BOAT_SLEEP_TIMEOUT"; then
    log "board is asleep. Wake it with: wol/boat-wake.sh"
else
    warn "board still answering after ${WAIT_ELAPSED}s - it did NOT suspend."
    warn "run 'wol/boat-sleep.sh --status' to see what boat-sleep objects to;"
    warn "the usual answer is that Wake-on-LAN could not be armed, and it"
    warn "refuses to sleep somewhere it cannot be woken from."
    exit 1
fi
