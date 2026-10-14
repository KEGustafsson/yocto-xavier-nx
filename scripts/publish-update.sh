#!/usr/bin/env bash
# Publish an A/B update bundle to an update channel (docs/07-updates.md).
#
#   scripts/publish-update.sh testing  <bundle.raucb>   # the bench
#   scripts/publish-update.sh stable   <bundle.raucb>   # the boat
#   BOAT_PUBLISH_DEST=/srv/www/boat-xavier-nx scripts/publish-update.sh ...
#   BOAT_PUBLISH_DEST=user@host:/srv/www/boat-xavier-nx scripts/publish-update.sh ...
#
# Reads the bundle's own manifest (version, compatible, L4T release), gives
# it a content-addressed name, writes the channel index and signs it with the
# index-signing key from scripts/gen-update-keys.sh into one attached
# index.p7, then uploads the bundle FIRST and the index LAST - so a board
# never sees an index naming a file that is not there yet.
#
# BOAT_PUBLISH_DEST is a local directory (a web server's document root) or an
# rsync target (host:path); either way it is the directory that holds one
# subdirectory per channel. For object storage, publish to a local directory
# and sync that with your provider's tool - in the same order.
#
# Options:
#   --reflash      mark the release as a BSP bump: boards are told to
#                  reflash at the dock instead of downloading it
#   --notes FILE   release notes shown by `boat-update status`
#
# Promote a release from testing to stable by publishing the same bundle to
# stable: names are content-addressed, so the file is never renamed.
#
# A bundle from a BOAT_BENCH build (its compatible ends in -bench) goes to
# the 'bench' channel only, and nothing else does: its rootfs trusts the
# bench CA (docs/plans/ab-validation.md).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }

REFLASH=false
NOTES=""
ARGS=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --reflash) REFLASH=true ;;
    --notes)   [[ "$#" -ge 2 ]] || die "--notes needs a file"; NOTES="$2"; shift ;;
    -h|--help) usage 0 ;;
    -*)        die "unknown option '$1' (see --help)" ;;
    *)         ARGS+=("$1") ;;
  esac
  shift
done
[[ "${#ARGS[@]}" -eq 2 ]] || usage 2
CHANNEL="${ARGS[0]}"
BUNDLE="${ARGS[1]}"
[[ "${CHANNEL}" =~ ^[a-z][a-z0-9-]*$ ]] || die "channel must be a simple name like stable or testing, got '${CHANNEL}'"
[[ -f "${BUNDLE}" ]] || die "no bundle at ${BUNDLE}"
: "${BOAT_PUBLISH_DEST:?set BOAT_PUBLISH_DEST to the channel root (a directory, or host:path for rsync)}"
need openssl
need unsquashfs
need python3

KEY="${BOAT_KEYS_DIR}/index-signing.key.pem"
CERT="${BOAT_KEYS_DIR}/index-signing.cert.pem"
[[ -f "${KEY}" && -f "${CERT}" ]] || die "no index-signing key in ${BOAT_KEYS_DIR} - run scripts/gen-update-keys.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# A verity bundle is a squashfs with the signature appended; the manifest is
# a file inside it.
unsquashfs -q -cat "${BUNDLE}" manifest.raucm > "${WORK}/manifest.raucm" 2>/dev/null \
  || die "cannot read manifest.raucm from ${BUNDLE} - is it a RAUC bundle?"

SHA256="$(sha256sum "${BUNDLE}" | cut -d' ' -f1)"
SIZE="$(stat -c %s "${BUNDLE}")"

# The serial only ever grows: a board rejects an index whose serial is not
# above the last one it accepted on that channel. It is the larger of the
# last serial + 1 and the time in seconds, so a lost or restored counter, or
# a second publishing machine, can still never go back below what boards
# have seen. A counter that is there but unreadable stops the publish.
SERIAL_FILE="${BOAT_KEYS_DIR}/index-serial"
LAST=0
if [[ -e "${SERIAL_FILE}" ]]; then
  LAST="$(tr -d '[:space:]' < "${SERIAL_FILE}")"
  [[ "${LAST}" =~ ^[0-9]+$ ]] \
    || die "${SERIAL_FILE} holds '${LAST}', not a number - fix it first: boards refuse an index whose serial does not grow"
fi
NOW="$(date +%s)"
SERIAL=$(( LAST + 1 > NOW ? LAST + 1 : NOW ))

python3 - "${WORK}/manifest.raucm" "${WORK}/index.json" "${CHANNEL}" "${SERIAL}" \
         "${SIZE}" "${SHA256}" "${REFLASH}" "${NOTES}" <<'PY'
import configparser, datetime, json, sys
manifest, out, channel, serial, size, sha, reflash, notes = sys.argv[1:]
cp = configparser.ConfigParser(interpolation=None, strict=False)
cp.read(manifest)
version = cp.get("update", "version")
doc = {
    "channel": channel,
    "serial": int(serial),
    "compatible": cp.get("update", "compatible"),
    "version": version,
    "l4t": cp.get("meta.boat", "l4t", fallback=""),
    "bundle": "boat-bundle-%s-%s.raucb" % (version, sha[:12]),
    "size": int(size),
    "sha256": sha,
    "published": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "requires_reflash": reflash == "true",
}
if not doc["l4t"]:
    sys.exit("the bundle has no [meta.boat] l4t= - it was not built by meta-boat's boat-bundle")
if doc["compatible"].endswith("-bench") and channel != "bench":
    sys.exit("%r is a BOAT_BENCH build (its rootfs trusts the bench CA) - it goes to the 'bench' "
             "channel only, never to %r" % (doc["compatible"], channel))
if channel == "bench" and not doc["compatible"].endswith("-bench"):
    sys.exit("the 'bench' channel is for BOAT_BENCH builds; this bundle is %r" % doc["compatible"])
if notes:
    with open(notes) as f:
        doc["notes"] = f.read()[:2000]
with open(out, "w") as f:
    json.dump(doc, f, indent=1, sort_keys=True)
PY
read -r VERSION NAME < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["version"], d["bundle"])' "${WORK}/index.json")

openssl cms -sign -nodetach -binary -outform DER \
  -signer "${CERT}" -inkey "${KEY}" -in "${WORK}/index.json" -out "${WORK}/index.p7"
# Check it the way a board will, before anything is uploaded.
openssl cms -verify -inform DER -binary -in "${WORK}/index.p7" -out /dev/null \
  -CAfile "${BOAT_KEYS_DIR}/ca.cert.pem" -no-CApath -no-CAstore -purpose smimesign -no_check_time 2>/dev/null \
  || die "the signed index does not verify against ${BOAT_KEYS_DIR}/ca.cert.pem"

# Record the serial before anything is uploaded, atomically: a publish that
# fails half way must not be followed by one that reuses its serial.
printf '%s\n' "${SERIAL}" > "${SERIAL_FILE}.tmp" && mv "${SERIAL_FILE}.tmp" "${SERIAL_FILE}"

log "publishing ${VERSION} to '${CHANNEL}' as ${NAME} (serial ${SERIAL}, $(( SIZE >> 20 )) MiB)"
if [[ "${BOAT_PUBLISH_DEST}" == *:* ]]; then
  need rsync
  # rsync to a temporary name and rename on the far side is what makes the
  # index appear in one step; --partial-dir does that for us.
  rsync --mkpath --partial-dir=.rsync-partial "${BUNDLE}" "${BOAT_PUBLISH_DEST}/${CHANNEL}/${NAME}"
  rsync --mkpath --partial-dir=.rsync-partial "${WORK}/index.p7" "${BOAT_PUBLISH_DEST}/${CHANNEL}/index.p7"
else
  DEST="${BOAT_PUBLISH_DEST}/${CHANNEL}"
  mkdir -p "${DEST}"
  cp "${BUNDLE}" "${DEST}/.${NAME}.tmp" && mv "${DEST}/.${NAME}.tmp" "${DEST}/${NAME}"
  cp "${WORK}/index.p7" "${DEST}/.index.p7.tmp" && mv "${DEST}/.index.p7.tmp" "${DEST}/index.p7"
fi
log "published. Boards following '${CHANNEL}' see it at their next check ('boat-update check' to ask now)."
