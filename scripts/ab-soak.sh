#!/usr/bin/env bash
# A/B soak test on a BENCH board (docs/plans/ab-validation.md): install good
# and faulty bundles in turn, over and over, and check that every good one
# commits and every faulty one falls back by itself.
#
#   scripts/ab-soak.sh --good GOOD.raucb --faulty F.raucb [--faulty F2.raucb ...]
#                      [--cycles 50] [--host root@bench-board] [--log soak.csv]
#   scripts/ab-soak.sh --ota DIR [--stream] [--cycles N] [--host ...] [--log ...]
#
# Over SSH (the default), odd cycles install the good bundle, even cycles
# the next faulty one (from boat-bundle-faulty,
# BOAT_FAULT=panic|emergency|tier2|hang, signed by the bench CA - so the
# board must run a BOAT_BENCH image).
#
# Over the air (--ota DIR), cycle N takes the Nth *.raucb in DIR by name;
# a name with "-faulty" in it is a faulty bundle, any other a good one. Each
# is published to the 'bench' channel with publish-update.sh
# (BOAT_PUBLISH_DEST must be set), and the board fetches it: `boat-update
# check`, `download` and `install --reboot`, or with --stream `check` and
# `install --stream --reboot`. A board takes only a release newer than the
# one it runs, so the versions must grow in name order (see
# docs/plans/ab-validation.md for a build loop). --cycles defaults to the
# number of bundles. Each cycle:
#
#   1. note the running slot and the kernel's boot ID;
#   2. copy the bundle to /data and `boat-update install --reboot` - or,
#      over the air, publish it and have the board fetch and install it;
#   3. wait for the board to go down and come back (a faulty bundle can take
#      ~25 minutes: the hang fault only ends at the 15-minute trial deadline);
#   4. wait until the running slot is committed;
#   5. pass if the board really rebooted (a new boot ID) and a good bundle
#      came back on the OTHER slot, or a faulty one on the SAME slot it
#      started from with the other one marked bad (OK=0) - not a bundle that
#      was refused before anything was written.
#
# Power cuts: with BOAT_SOAK_POWER_CUT_CMD set (e.g. a USB relay on the
# barrel jack: "relay off; sleep 3; relay on"), every Nth cycle
# (--power-cut-every N) runs it at a random moment after the install
# starts. Such a cycle waits for the cut to have happened, then only has
# to end with the board up and committed on either slot - exactly what a
# power cut may legitimately do. A cut command that fails fails the cycle.
#
# Results go to a CSV (default ab-soak-<time>.csv), with how each bundle
# was delivered (ssh, ota or ota-stream) so the two soaks are recorded
# apart; the exit status is the number of failed cycles (capped at 100).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../wol/common.sh
source "${HERE}/../wol/common.sh"

GOOD=""
FAULTY=()
CYCLES=""
OTA_DIR=""
STREAM=no
TARGET=""
LOG="ab-soak-$(date -u +%Y%m%dT%H%M%SZ).csv"
CUT_EVERY=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --good)   GOOD="$2"; shift ;;
    --faulty) FAULTY+=("$2"); shift ;;
    --cycles) CYCLES="$2"; shift ;;
    --host)   TARGET="$2"; shift ;;
    --log)    LOG="$2"; shift ;;
    --power-cut-every) CUT_EVERY="$2"; shift ;;
    --ota)    OTA_DIR="$2"; shift ;;
    --stream) STREAM=yes ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument '$1' (see --help)" ;;
  esac
  shift
done
if [[ -n "${OTA_DIR}" ]]; then
  [[ -z "${GOOD}" && "${#FAULTY[@]}" -eq 0 ]] || die "--ota takes its bundles from the directory, not --good/--faulty"
  [[ -d "${OTA_DIR}" ]] || die "--ota: no directory '${OTA_DIR}'"
  mapfile -t OTA_BUNDLES < <(find "${OTA_DIR}" -maxdepth 1 -type f -name '*.raucb' | LC_ALL=C sort)
  [[ "${#OTA_BUNDLES[@]}" -gt 0 ]] || die "--ota: no .raucb files in '${OTA_DIR}'"
  [[ -n "${BOAT_PUBLISH_DEST:-}" ]] || die "--ota publishes each bundle to the bench channel: set BOAT_PUBLISH_DEST"
  : "${CYCLES:=${#OTA_BUNDLES[@]}}"
  [[ ! "${CYCLES}" =~ ^[0-9]+$ || "${CYCLES}" -le "${#OTA_BUNDLES[@]}" ]] \
    || die "--cycles ${CYCLES}, but '${OTA_DIR}' has only ${#OTA_BUNDLES[@]} bundles - over the air, each is used once"
  DELIVERY=ota
  [[ "${STREAM}" == yes ]] && DELIVERY=ota-stream
else
  [[ "${STREAM}" == no ]] || die "--stream needs --ota"
  [[ -f "${GOOD}" ]] || die "--good: no bundle at '${GOOD}'"
  [[ "${#FAULTY[@]}" -gt 0 ]] || die "at least one --faulty bundle is needed"
  for f in "${FAULTY[@]}"; do [[ -f "$f" ]] || die "--faulty: no bundle at '$f'"; done
  : "${CYCLES:=50}"
  DELIVERY=ssh
fi
[[ "${CYCLES}" =~ ^[0-9]+$ && "${CYCLES}" -gt 0 ]] || die "--cycles must be a positive number"
[[ "${CUT_EVERY}" =~ ^[0-9]+$ ]] || die "--power-cut-every must be a number"
if (( CUT_EVERY > 0 )); then
  [[ -n "${BOAT_SOAK_POWER_CUT_CMD:-}" ]] || die "--power-cut-every needs BOAT_SOAK_POWER_CUT_CMD"
fi
if [[ -z "${TARGET}" ]]; then
  require_conf BOAT_HOST
  TARGET="${BOAT_SSH_USER}@${BOAT_HOST}"
else
  BOAT_HOST="${TARGET#*@}"
fi
need ssh

# ServerAlive* ends a session whose board has gone (a power cut mid-copy or
# mid-install) after a minute, instead of TCP's retransmission timeout. A
# board that is busy still answers them, so a long copy or install is fine.
# shellcheck disable=SC2086 # BOAT_SSH_OPTS is a list of options
ssh_b() {
  ssh ${BOAT_SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "${TARGET}" "$@"
}

# What the board runs to fetch and install a published bundle. A check or
# download can meet the board's own timer holding the lock, so each gets a
# few tries.
ota_install_cmd() {
  local retry='try() { n=0; while ! "$@"; do n=$((n + 1)); [ "$n" -ge 6 ] && return 1; sleep 20; done; }'
  if [[ "${STREAM}" == yes ]]; then
    echo "${retry}; try boat-update check && boat-update install --stream --reboot"
  else
    echo "${retry}; try boat-update check && try boat-update download && boat-update install --reboot"
  fi
}

slot_now() { ssh_b 'sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline' 2>/dev/null; }

boot_id() { ssh_b 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null; }

# The other slot is marked bad: where a faulty bundle must leave it.
other_bad() {
  ssh_b 's=$(sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline); o=$([ "$s" = A ] && echo B || echo A); grub-editenv /boot/efi/EFI/BOOT/grubenv list | grep -qx "${o}_OK=0"' 2>/dev/null
}

committed_now() {
  ssh_b 's=$(sed -n "s/.*rauc\.slot=\([AB]\).*/\1/p" /proc/cmdline); grub-editenv /boot/efi/EFI/BOOT/grubenv list | grep -qx "${s}_COMMITTED=1"' 2>/dev/null
}

# Wait for SSH to answer and the running slot to be committed.
settle() {
  local limit="$1" started=$SECONDS
  while (( SECONDS - started < limit )); do
    if committed_now; then return 0; fi
    sleep 10
  done
  return 1
}

ssh_b 'boat-update status' >/dev/null || die "cannot run boat-update on ${TARGET}"
ssh_b 'grep -qx "compatible=.*-bench" /etc/rauc/system.conf' \
  || die "${TARGET} does not run a BOAT_BENCH image - it would refuse the fault bundles before writing anything"
settle 900 || die "the board is not on a committed slot - start from a settled board"

echo "cycle,delivery,bundle,expect,power_cut,slot_before,slot_after,committed,result,seconds" > "${LOG}"
failed=0
fi_idx=0
for (( c = 1; c <= CYCLES; c++ )); do
  if [[ -n "${OTA_DIR}" ]]; then
    bundle="${OTA_BUNDLES[c - 1]}"
    case "$(basename "${bundle}")" in
      *-faulty*) expect=stay ;;
      *)         expect=switch ;;
    esac
  elif (( c % 2 == 1 )); then
    bundle="${GOOD}"; expect=switch
  else
    bundle="${FAULTY[fi_idx % ${#FAULTY[@]}]}"; expect=stay
    fi_idx=$(( fi_idx + 1 ))
  fi
  cut=no
  if (( CUT_EVERY > 0 && c % CUT_EVERY == 0 )); then cut=yes; fi
  started=$SECONDS
  after=""
  result=FAIL
  # A board that did not come back last cycle gets one more chance here; a
  # cycle that cannot even start is a failed one, not the end of the run.
  if ! settle 2400; then
    before=""
    err "cycle ${c}: the board is not up on a committed slot - skipping the cycle"
  else
    before="$(slot_now || true)"
    boot="$(boot_id || true)"
    note=""
    [[ "${cut}" == yes ]] && note=", power cut"
    log "cycle ${c}/${CYCLES}: $(basename "${bundle}") on slot ${before} (expect: ${expect}${note})"
    install_cmd=""
    if [[ -n "${OTA_DIR}" ]]; then
      if "${HERE}/publish-update.sh" bench "${bundle}" >/dev/null; then
        install_cmd="$(ota_install_cmd)"
      else
        err "  could not publish the bundle to the bench channel"
      fi
    else
      remote="/data/boat-update/incoming/$(basename "${bundle}")"
      # Copied over ssh rather than scp: BOAT_SSH_OPTS is written for ssh
      # (-p, not scp's -P).
      if ssh_b "mkdir -p /data/boat-update/incoming && rm -f /data/boat-update/incoming/*.raucb && cat > '${remote}'" < "${bundle}"; then
        install_cmd="boat-update install '${remote}' --reboot"
      else
        err "  could not copy the bundle to the board"
      fi
    fi
    if [[ -n "${install_cmd}" ]]; then
      if [[ "${cut}" == yes ]]; then
        delay=$(( RANDOM % 240 + 5 ))
        ( sleep "${delay}"; log "  power cut after ${delay}s"; sh -c "${BOAT_SOAK_POWER_CUT_CMD}" ) &
        cutter=$!
      fi
      out="$(ssh_b "${install_cmd}" 2>&1)" || true
      # A cut can come after the board has already rebooted by itself, so
      # wait for it first: what is checked below is the board after the cut.
      if [[ "${cut}" == yes ]] && ! wait "${cutter}"; then
        cut=failed
        err "  the power-cut command failed (BOAT_SOAK_POWER_CUT_CMD)"
      fi
      wait_for_state down 600 >/dev/null || true
      if ! wait_for_state up 2400 >/dev/null || ! settle 2400; then
        after="$(slot_now || true)"
        err "  the board did not come back to a committed slot"
      else
        after="$(slot_now || true)"
        rebooted=no
        [[ -n "${boot}" && "$(boot_id || true)" != "${boot}" ]] && rebooted=yes
        if [[ "${rebooted}" != yes ]]; then
          err "  the board never rebooted - the install was refused or never ran:"
          tail -n 5 <<< "${out}" | sed 's/^/    /' >&2
        elif [[ "${cut}" == failed ]]; then
          :
        elif [[ "${cut}" == yes ]]; then
          result=PASS
        elif [[ "${expect}" == switch && -n "${after}" && "${after}" != "${before}" ]]; then
          result=PASS
        elif [[ "${expect}" == stay && "${after}" == "${before}" ]] && other_bad; then
          result=PASS
        fi
      fi
    fi
  fi
  committed=no
  committed_now && committed=yes
  [[ "${result}" == PASS ]] || failed=$(( failed + 1 ))
  echo "${c},${DELIVERY},$(basename "${bundle}"),${expect},${cut},${before:-?},${after:-?},${committed},${result},$(( SECONDS - started ))" >> "${LOG}"
  log "  ${result}: slot ${before:-?} -> ${after:-?} in $(( SECONDS - started ))s"
done
log "${CYCLES} cycles, ${failed} failed - details in ${LOG}"
ssh_b 'boat-update history' || true
exit $(( failed > 100 ? 100 : failed ))
