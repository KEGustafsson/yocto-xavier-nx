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
# the board to come back - over SSH, with a new boot ID, so a jump host or
# a Host alias in ~/.ssh/config works as for any ssh - and reports which
# slot it came back on: the new one (on trial - boat-commit decides) or, if
# the trial failed and GRUB fell back, the old one. The copy on /data is
# removed again whatever happens.
#
# The board's user is root, or one with passwordless sudo (wol/README.md:
# BOAT_SSH_USER=boat): its commands then run through `sudo -n`.
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
else
  require_conf BOAT_HOST
  TARGET="${BOAT_SSH_USER}@${BOAT_HOST}"
fi
need ssh

# ServerAlive* ends a session whose board has gone (a power cut mid-copy or
# mid-install) after a minute, instead of TCP's retransmission timeout. A
# board that is busy still answers them, so a long copy or install is fine.
# shellcheck disable=SC2086 # BOAT_SSH_OPTS is a list of options
ssh_b() {
  ssh ${BOAT_SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=15 \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "${TARGET}" "$@"
}
boot_id() { ssh_b 'cat /proc/sys/kernel/random/boot_id'; }

log "checking ${TARGET} ..."
ssh_b 'boat-update status' || die "cannot run boat-update on ${TARGET} - is it an A/B image?"
# Installing needs root on the board: as another user (the boat account),
# through its passwordless sudo, the way wol/boat-sleep.sh does.
SUDO=""
if [[ "$(ssh_b 'id -u')" != 0 ]]; then
  SUDO="sudo -n "
  ssh_b 'sudo -n true' \
    || die "${TARGET} is not root and has no passwordless sudo - installing needs root (give root@host, or BOAT_SSH_USER=root)"
fi
BEFORE="$(ssh_b 'sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline')"
BOOT_BEFORE="$(boot_id)" || die "cannot read the board's boot ID"

# The name goes into a command line the board's shell runs as root: only a
# plain bundle name, never one that could end the quoting around it.
NAME="$(basename "${BUNDLE}")"
[[ "${NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*\.raucb$ ]] \
  || die "'${NAME}' is not a plain bundle file name (letters, digits, . _ + -, ending .raucb) - rename it first"
REMOTE="/data/boat-update/incoming/${NAME}"
# A bundle is several GB on /data, which Docker and the journal share and
# boat-update's downloads need room on: it goes again once installed, or
# when anything fails. Only this push's own files: another push may be
# copying or installing its bundle beside it.
remove_copy() { ssh_b "${SUDO}rm -f '${REMOTE}' '${REMOTE}.part'" || true; }
log "copying $(basename "${BUNDLE}") ($(( $(stat -c %s "${BUNDLE}") >> 20 )) MiB) ..."
trap remove_copy EXIT
# Over ssh rather than scp: BOAT_SSH_OPTS is written for ssh (-p, not scp's
# -P; -l is a login there, a bandwidth limit for scp). A copy cut off half
# way is never taken for a whole bundle: it is renamed only once complete.
# Copies an earlier run left (a cut connection, a power cut) go too - only
# ones untouched for a day, never a push still in progress.
ssh_b "${SUDO}mkdir -p /data/boat-update/incoming \
       && ${SUDO}find /data/boat-update/incoming -maxdepth 1 -type f \( -name '*.raucb' -o -name '*.raucb.part' \) -mmin +1440 -delete \
       && ${SUDO}rm -f '${REMOTE}' '${REMOTE}.part' \
       && ${SUDO}sh -c \"cat > '${REMOTE}.part'\" \
       && ${SUDO}mv '${REMOTE}.part' '${REMOTE}'" < "${BUNDLE}" \
  || die "could not copy the bundle to ${TARGET}:${REMOTE}"

if (( REBOOT == 0 )); then
  ssh_b "${SUDO}boat-update install '${REMOTE}'"
  log "installed; the other slot boots as a trial on the next reboot."
  exit 0
fi

log "installing and rebooting ..."
# The connection drops when the board reboots (ssh exits 255); that is the
# expected end. Any other failure is boat-update refusing or failing the
# install - the board is not going to reboot, so say why now.
rc=0
ssh_b "${SUDO}boat-update install '${REMOTE}' --reboot" || rc=$?
if (( rc != 0 && rc != 255 )); then
  die "the install failed on ${TARGET} (exit ${rc}, see above) - the board stays on slot ${BEFORE}"
fi

log "waiting for the board to reboot and come back ..."
wait_for_new_boot "${BOOT_BEFORE}" 1200 \
  || die "no new boot over SSH 20 minutes after the install - if it never rebooted, check 'boat-update status' on it; a trial that fails falls back by itself, so give it time, then look at the console"
AFTER="$(ssh_b 'sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline')" || die "the board came back, but SSH stopped answering"
if [[ "${AFTER}" != "${BEFORE}" ]]; then
  log "the board came back on slot ${AFTER} - the new release, on trial until boat-commit passes it:"
else
  warn "the board came back on slot ${AFTER}, the OLD one: the new release did not pass its trial."
fi
ssh_b 'boat-update status' || true
