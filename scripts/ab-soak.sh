#!/usr/bin/env bash
# A/B soak test on a BENCH board (docs/plans/ab-validation.md): install good
# and faulty bundles in turn, over and over, and check that every good one
# commits and every faulty one falls back by itself.
#
#   scripts/ab-soak.sh --good GOOD.raucb --faulty F.raucb [--faulty F2.raucb ...]
#                      [--cycles 50] [--host root@bench-board] [--log soak.csv]
#
# Odd cycles install the good bundle, even cycles the next faulty one (from
# boat-bundle-faulty, BOAT_FAULT=panic|emergency|tier2|hang, signed by the
# bench CA - so the board must run a BOAT_BENCH image). Each cycle:
#
#   1. note the running slot and the kernel's boot ID;
#   2. copy the bundle to /data and `boat-update install --reboot`;
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
# starts. Such a cycle only has to end with the board up and committed on
# either slot - exactly what a power cut may legitimately do.
#
# Results go to a CSV (default ab-soak-<time>.csv); the exit status is the
# number of failed cycles (capped at 100).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../wol/common.sh
source "${HERE}/../wol/common.sh"

GOOD=""
FAULTY=()
CYCLES=50
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
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument '$1' (see --help)" ;;
  esac
  shift
done
[[ -f "${GOOD}" ]] || die "--good: no bundle at '${GOOD}'"
[[ "${#FAULTY[@]}" -gt 0 ]] || die "at least one --faulty bundle is needed"
for f in "${FAULTY[@]}"; do [[ -f "$f" ]] || die "--faulty: no bundle at '$f'"; done
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

# shellcheck disable=SC2086 # BOAT_SSH_OPTS is a list of options
ssh_b() { ssh ${BOAT_SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=10 "${TARGET}" "$@"; }

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

echo "cycle,bundle,expect,power_cut,slot_before,slot_after,committed,result,seconds" > "${LOG}"
failed=0
fi_idx=0
for (( c = 1; c <= CYCLES; c++ )); do
  if (( c % 2 == 1 )); then
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
    remote="/data/boat-update/incoming/$(basename "${bundle}")"
    # Copied over ssh rather than scp: BOAT_SSH_OPTS is written for ssh
    # (-p, not scp's -P).
    if ! ssh_b "mkdir -p /data/boat-update/incoming && rm -f /data/boat-update/incoming/*.raucb && cat > '${remote}'" < "${bundle}"; then
      err "  could not copy the bundle to the board"
    else
      if [[ "${cut}" == yes ]]; then
        delay=$(( RANDOM % 240 + 5 ))
        ( sleep "${delay}"; log "  power cut after ${delay}s"; sh -c "${BOAT_SOAK_POWER_CUT_CMD}" ) &
        cutter=$!
      fi
      ssh_b "boat-update install '${remote}' --reboot" >/dev/null 2>&1 || true
      wait_for_state down 600 >/dev/null || true
      if ! wait_for_state up 2400 >/dev/null || ! settle 2400; then
        after="$(slot_now || true)"
        err "  the board did not come back to a committed slot"
      else
        after="$(slot_now || true)"
        rebooted=no
        [[ -n "${boot}" && "$(boot_id || true)" != "${boot}" ]] && rebooted=yes
        if [[ "${rebooted}" != yes ]]; then
          err "  the board never rebooted - the install was refused or never ran"
        elif [[ "${cut}" == yes ]]; then
          result=PASS
        elif [[ "${expect}" == switch && -n "${after}" && "${after}" != "${before}" ]]; then
          result=PASS
        elif [[ "${expect}" == stay && "${after}" == "${before}" ]] && other_bad; then
          result=PASS
        fi
      fi
      if [[ "${cut}" == yes ]]; then wait "${cutter}" 2>/dev/null || true; fi
    fi
  fi
  committed=no
  committed_now && committed=yes
  [[ "${result}" == PASS ]] || failed=$(( failed + 1 ))
  echo "${c},$(basename "${bundle}"),${expect},${cut},${before:-?},${after:-?},${committed},${result},$(( SECONDS - started ))" >> "${LOG}"
  log "  ${result}: slot ${before:-?} -> ${after:-?} in $(( SECONDS - started ))s"
done
log "${CYCLES} cycles, ${failed} failed - details in ${LOG}"
ssh_b 'boat-update history' || true
exit $(( failed > 100 ? 100 : failed ))
