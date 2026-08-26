# shellcheck shell=bash
# Shared configuration for the wol/ helpers. Sourced, never run directly.
#
# Values come from, in order of precedence:
#   1. the environment            BOAT_HOST=... wol/boat-wake.sh
#   2. wol/boat.conf              your own copy, git-ignored
#   3. the defaults below         placeholders, deliberately unusable
#
# boat.conf is git-ignored on purpose: it holds this particular boat's MAC
# and LAN addresses, which are device-identifying and have no business in a
# repository that gets pushed. Copy boat.conf.example and fill it in.

set -euo pipefail

WOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${WOL_DIR}/.." && pwd)"

# log/warn/err/die/need/confirm and the "environment beats boat.conf" loader
# all live in scripts/lib.sh - one definition, so the two halves of this repo
# cannot drift into printing differently or resolving config differently.
# shellcheck source=../scripts/lib.sh
source "${REPO_ROOT}/scripts/lib.sh"

load_boat_conf "${WOL_DIR}/boat.conf"

: "${BOAT_HOST:=}"
: "${BOAT_MAC:=}"
: "${BOAT_BROADCAST:=255.255.255.255}"
: "${BOAT_SSH_USER:=root}"
# Extra ssh options, word-split on purpose: a jump host, an identity file,
# a non-standard port, or a separate known_hosts while a board is being
# reflashed and its host key keeps changing. Example:
#   BOAT_SSH_OPTS="-i $HOME/.ssh/boat_ed25519 -p 2222"
# Write $HOME rather than ~ : the value is word-split, not tilde-expanded, so
# a bare ~ reaches ssh literally. It happens to work for IdentityFile and
# UserKnownHostsFile (ssh expands those itself) and fails confusingly for
# anything else, e.g. -F.
: "${BOAT_SSH_OPTS:=}"
# How long boat-wake.sh waits for the board to answer before giving up. A
# resume from SC7 took about 7 seconds on a Xavier NX devkit; 90 leaves room
# for a slow switch to re-negotiate the link.
: "${BOAT_WAKE_TIMEOUT:=90}"
# How long boat-sleep.sh waits for the board to actually go quiet.
: "${BOAT_SLEEP_TIMEOUT:=60}"

# Sender knobs, read by scripts/wake-boat.sh in a CHILD process - so they have
# to be exported, not merely set. boat.conf is a file of plain assignments, so
# without this a BOAT_WOL_COUNT set there reached nothing.
: "${BOAT_WOL_BROADCAST:=${BOAT_BROADCAST}}"
: "${BOAT_WOL_PORT:=9}"
: "${BOAT_WOL_COUNT:=3}"
export BOAT_HOST BOAT_MAC BOAT_BROADCAST BOAT_SSH_USER BOAT_SSH_OPTS \
       BOAT_WAKE_TIMEOUT BOAT_SLEEP_TIMEOUT \
       BOAT_WOL_BROADCAST BOAT_WOL_PORT BOAT_WOL_COUNT

# A timeout that is not a whole number of seconds turns every wait below into
# an instant no-op - `(( elapsed < 90s ))` fails on its first evaluation - and
# the caller is then handed a "the board never answered" diagnosis for what is
# really a typo in boat.conf. Catch it here, the way the target-side
# boat-sleep already validates its own delay.
for _t in BOAT_WAKE_TIMEOUT BOAT_SLEEP_TIMEOUT; do
    case "${!_t}" in
        ''|*[!0-9]*) err "${_t} must be a whole number of seconds, got '${!_t}'"
                     err "check wol/boat.conf, or the value in your environment"
                     exit 1 ;;
    esac
done
unset _t

require_conf() {
    local missing=() v
    for v in "$@"; do
        [[ -n "${!v:-}" ]] || missing+=("$v")
    done
    if (( ${#missing[@]} )); then
        err "not configured: ${missing[*]}"
        err "copy wol/boat.conf.example to wol/boat.conf and fill it in,"
        err "or pass them in the environment, e.g.:"
        err "    BOAT_HOST=192.168.0.43 BOAT_MAC=48:b0:2d:11:22:33 $0"
        exit 1
    fi
}

# Ping is only ever used as a liveness probe here, never as proof of
# anything else - a board that answers ICMP has resumed far enough to matter.
boat_is_up() { ping -c1 -W1 "$BOAT_HOST" >/dev/null 2>&1; }

# Wait for boat_is_up to become $1 ("up"/"down"), up to $2 seconds. Prints a
# dot per probe so a long wait does not look like a hang.
#
# The deadline is against the WALL CLOCK, not a count of loop iterations: each
# pass is a `ping -W1` (up to a second against an unreachable host) plus a one
# second sleep, so counting sleeps made the real timeout roughly twice the
# configured one - and the "no answer after 90s" message that followed was off
# by the same factor. SECONDS is bash's own counter and needs no date(1).
#
# WAIT_ELAPSED is left holding the number of seconds actually spent, so the
# caller's give-up message can report what happened rather than what was asked
# for.
WAIT_ELAPSED=0
# shellcheck disable=SC2034  # WAIT_ELAPSED is read by the sourcing scripts
wait_for_state() {
    local want="$1" limit="$2" started=$SECONDS probes=0 state
    while (( SECONDS - started < limit )); do
        if boat_is_up; then state=up; else state=down; fi
        if [[ "$state" == "$want" ]]; then
            (( probes > 0 )) && printf '\n'
            WAIT_ELAPSED=$(( SECONDS - started ))
            return 0
        fi
        printf '.'
        probes=$((probes + 1))
        sleep 1
    done
    (( probes > 0 )) && printf '\n'
    WAIT_ELAPSED=$(( SECONDS - started ))
    return 1
}
