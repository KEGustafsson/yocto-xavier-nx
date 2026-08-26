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

_c()   { [[ -t 1 ]] && printf '\033[%sm' "$1" || true; }
log()  { printf '%s[+]%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn() { printf '%s[!]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
err()  { printf '%s[-]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }
die()  { err "$*"; exit 1; }

# shellcheck source=/dev/null
[[ -r "${WOL_DIR}/boat.conf" ]] && source "${WOL_DIR}/boat.conf"

: "${BOAT_HOST:=}"
: "${BOAT_MAC:=}"
: "${BOAT_BROADCAST:=255.255.255.255}"
: "${BOAT_SSH_USER:=root}"
# Extra ssh options, word-split on purpose: a jump host, an identity file,
# a non-standard port, or a separate known_hosts while a board is being
# reflashed and its host key keeps changing. Example:
#   BOAT_SSH_OPTS="-i ~/.ssh/boat_ed25519 -p 2222"
: "${BOAT_SSH_OPTS:=}"
# How long boat-wake.sh waits for the board to answer before giving up. A
# resume from SC7 took about 7 seconds on a Xavier NX devkit; 90 leaves room
# for a slow switch to re-negotiate the link.
: "${BOAT_WAKE_TIMEOUT:=90}"
# How long boat-sleep.sh waits for the board to actually go quiet.
: "${BOAT_SLEEP_TIMEOUT:=60}"

require_conf() {
    local missing=()
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
# dot per second so a long wait does not look like a hang.
wait_for_state() {
    local want="$1" limit="$2" waited=0 state
    while (( waited < limit )); do
        if boat_is_up; then state=up; else state=down; fi
        if [[ "$state" == "$want" ]]; then
            (( waited > 0 )) && printf '\n'
            return 0
        fi
        printf '.'
        sleep 1
        waited=$((waited + 1))
    done
    printf '\n'
    return 1
}
