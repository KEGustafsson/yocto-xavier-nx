#!/usr/bin/env bash
# Phase 0 of the A/B update plan: capture, read-only, what this board actually
# does before anything is built on top of assumptions about it.
#
#   scripts/ab-phase0-capture.sh                      # board from wol/boat.conf
#   scripts/ab-phase0-capture.sh root@192.168.0.43    # or name it
#   scripts/ab-phase0-capture.sh --channel-url https://updates.example/boat/ ...
#
# Pipes scripts/ab-phase0-probe.sh to the board over SSH and saves the
# whole transcript under yocto/ab-phase0/, then prints the probe's summary.
# The probe is read-only by construction - it writes nothing on the board,
# sets no UEFI variable and restarts nothing - so this is safe to run against
# the boat itself, not only a bench unit.
#
# --channel-url additionally checks, from this host, that the server you mean
# to publish OTA updates on answers HTTP Range requests the way RAUC probes
# them (206 Partial Content with a known total size). Both resumable downloads
# and RAUC streaming depend on it (plan section 5.10).
#
# The board is the one wol/boat.conf names (BOAT_HOST, BOAT_SSH_USER,
# BOAT_SSH_OPTS), unless one is given on the command line.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "${HERE}/env.sh"
# BOAT_HOST / BOAT_SSH_USER / BOAT_SSH_OPTS, with the same environment-beats-
# boat.conf precedence as the wol/ tools - one place configures the board.
# shellcheck source=../wol/common.sh
source "${REPO_ROOT}/wol/common.sh"

TARGET=""
CHANNEL_URL=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel-url) CHANNEL_URL="${2:-}"; [[ -n "$CHANNEL_URL" ]] || die "--channel-url needs a URL"; shift ;;
        -h|--help)     sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)            die "unknown option '$1'" ;;
        *)             TARGET="$1" ;;
    esac
    shift
done
if [[ -z "$TARGET" ]]; then
    require_conf BOAT_HOST
    TARGET="${BOAT_SSH_USER}@${BOAT_HOST}"
fi

need ssh
PROBE="${HERE}/ab-phase0-probe.sh"
[[ -r "$PROBE" ]] || die "missing ${PROBE}"

OUTDIR="${WORKROOT}/ab-phase0"
mkdir -p "$OUTDIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUTDIR}/${TARGET//[^A-Za-z0-9._-]/_}-${STAMP}.txt"

log "probing ${TARGET} (read-only) ..."
# BOAT_SSH_OPTS is word-split on purpose, exactly as in wol/ (see common.sh).
# shellcheck disable=SC2086
if ! ssh ${BOAT_SSH_OPTS} -o BatchMode=yes -o ConnectTimeout=15 "$TARGET" sh -s <"$PROBE" >"$OUT" 2>&1; then
    err "the probe did not complete - partial transcript in ${OUT}"
    tail -n 20 "$OUT" >&2 || true
    exit 1
fi

if [[ -n "$CHANNEL_URL" ]]; then
    need curl
    # Probe exactly the way RAUC's streaming helper does (nbd.c): a GET for
    # the first 4 bytes. It needs 206 Partial Content AND a Content-Range
    # whose total is a number - "bytes 0-3/*" (size unknown) is refused, and
    # so is a 200 that ignores the Range header and sends the whole file.
    hdrs="$(curl -s -o /dev/null -D - -r 0-3 "$CHANNEL_URL" || true)"
    code="$(printf '%s\n' "$hdrs" | awk 'toupper($1) ~ /^HTTP\// {c=$2} END {print c}')"
    crange="$(printf '%s\n' "$hdrs" | tr -d '\r' | awk 'tolower($1)=="content-range:" {print $2, $3}' | tail -n 1)"
    {
        printf '\n=== OTA channel Range support (from this host)\n'
        printf 'url: %s\n' "$CHANNEL_URL"
        printf 'GET Range: bytes=0-3 -> HTTP %s, Content-Range: %s\n' "${code:-none}" "${crange:-none}"
        if [[ "$code" == 206 && "$crange" =~ /[0-9]+$ ]]; then
            printf 'Q9 channel host supports Range the way RAUC needs: yes\n'
        elif [[ "$code" == 200 ]]; then
            printf 'Q9 channel host supports Range the way RAUC needs: NO - it returned the whole file (200)\n'
        elif [[ "$code" == 206 ]]; then
            printf 'Q9 channel host supports Range the way RAUC needs: NO - total size not given (%s)\n' "${crange:-no Content-Range}"
        else
            printf 'Q9 channel host supports Range the way RAUC needs: unknown (HTTP %s) - point --channel-url at an existing file\n' "${code:-none}"
        fi
    } >>"$OUT"
fi

log "transcript: ${OUT}"
# Echo the summary - the Q lines are what the plan's Phase 0 asks; the rest
# of the transcript is the evidence behind them.
sed -n '/^--- SUMMARY/,$p' "$OUT"
log "Next: the manual checks in docs/plans/ab-phase0-runbook.md (bench unit only)."
