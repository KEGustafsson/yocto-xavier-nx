#!/usr/bin/env bash
# Install an A/B update bundle on the boat over SSH - the fallback for when
# the boat has no internet (docs/07-updates.md).
#
#   scripts/push-update.sh <bundle.raucb>            # the board in wol/boat.conf
#   scripts/push-update.sh <bundle.raucb> root@host  # or name it
#   scripts/push-update.sh --no-reboot <bundle.raucb>
#
# Copies the bundle to /data/boat-update/incoming on the board, installs it
# into the other slot with `boat-update install --reboot`, then waits for
# the board to go down and come back, and reports which slot it came back
# on: the new one (on trial - boat-commit decides) or, if the trial failed
# and GRUB fell back, the old one.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../wol/common.sh
source "${HERE}/../wol/common.sh"

REBOOT=1
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-reboot) REBOOT=0 ;;
    -h|--help)   awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    -*)          die "unknown option '$arg'" ;;
    *)           ARGS+=("$arg") ;;
  esac
done
[[ "${#ARGS[@]}" -ge 1 && "${#ARGS[@]}" -le 2 ]] || die "usage: $0 [--no-reboot] <bundle.raucb> [user@host]"
BUNDLE="${ARGS[0]}"
[[ -f "${BUNDLE}" ]] || die "no bundle at ${BUNDLE}"
if [[ "${#ARGS[@]}" -eq 2 ]]; then
  TARGET="${ARGS[1]}"
  # A pattern such as boat-bundle-*.raucb matches the deploy directory's
  # link as well as the bundle: the second file is not a board.
  [[ ! -e "${TARGET}" ]] || die "${TARGET} is a file, not a board - give one bundle, e.g. the deploy directory's boat-bundle-<MACHINE>.raucb link"
  BOAT_HOST="${TARGET#*@}"
else
  require_conf BOAT_HOST
  TARGET="${BOAT_SSH_USER}@${BOAT_HOST}"
fi
need ssh
need scp

# shellcheck disable=SC2086 # BOAT_SSH_OPTS is a list of options
ssh_b() { ssh ${BOAT_SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=15 "${TARGET}" "$@"; }

log "checking ${TARGET} ..."
ssh_b 'boat-update status' || die "cannot run boat-update on ${TARGET} - is it an A/B image?"
BEFORE="$(ssh_b 'sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline')"

# The name goes into a command line the board's shell runs as root: only a
# plain bundle name, never one that could end the quoting around it.
NAME="$(basename "${BUNDLE}")"
[[ "${NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*\.raucb$ ]] \
  || die "'${NAME}' is not a plain bundle file name (letters, digits, . _ + -, ending .raucb) - rename it first"
REMOTE="/data/boat-update/incoming/${NAME}"
log "copying $(basename "${BUNDLE}") ($(( $(stat -c %s "${BUNDLE}") >> 20 )) MiB) ..."
ssh_b "mkdir -p /data/boat-update/incoming"
# shellcheck disable=SC2086
scp ${BOAT_SSH_OPTS} -o BatchMode=yes "${BUNDLE}" "${TARGET}:${REMOTE}"

if (( REBOOT == 0 )); then
  ssh_b "boat-update install '${REMOTE}'"
  log "installed; the other slot boots as a trial on the next reboot."
  exit 0
fi

log "installing and rebooting ..."
# The connection drops when the board reboots (ssh exits 255); that is the
# expected end. Any other failure is boat-update refusing or failing the
# install - the board is not going to reboot, so say why now.
rc=0
ssh_b "boat-update install '${REMOTE}' --reboot" || rc=$?
if (( rc != 0 && rc != 255 )); then
  die "the install failed on ${TARGET} (exit ${rc}, see above) - the board stays on slot ${BEFORE}"
fi

log "waiting for ${BOAT_HOST} to go down ..."
wait_for_state down 300 || die "the board did not reboot within 5 minutes - check 'boat-update status' on it"
log "waiting for it to come back ..."
wait_for_state up 900 || die "no answer 15 minutes after the reboot - a trial that fails falls back by itself; give it time, then look at the console"
for _ in $(seq 1 60); do
  ssh_b true 2>/dev/null && break
  sleep 5
done
AFTER="$(ssh_b 'sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline')" || die "the board answers ping but not SSH"
if [[ "${AFTER}" != "${BEFORE}" ]]; then
  log "the board came back on slot ${AFTER} - the new release, on trial until boat-commit passes it:"
else
  warn "the board came back on slot ${AFTER}, the OLD one: the new release did not pass its trial."
fi
ssh_b 'boat-update status' || true
ssh_b 'rm -f /data/boat-update/incoming/*.raucb' || true
