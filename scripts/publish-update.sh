#!/usr/bin/env bash
# Publish an A/B update bundle to an update channel (docs/07-updates.md).
#
#   scripts/publish-update.sh testing  <bundle.raucb>   # a test board, on a boat image
#   scripts/publish-update.sh stable   <bundle.raucb>   # the boats
#   scripts/publish-update.sh bench    <bundle.raucb>   # a BOAT_BENCH build, for the bench
#   BOAT_PUBLISH_DEST=/srv/www/boat-xavier-nx scripts/publish-update.sh ...
#   BOAT_PUBLISH_DEST=user@host:/srv/www/boat-xavier-nx scripts/publish-update.sh ...
#
# Checks the bundle the way a board's RAUC will - a verity bundle, signed
# by a bundle-signing certificate under the update CA - and reads its
# signed manifest (version, compatible, L4T release). Gives it a
# content-addressed name, writes the channel index and signs it with the
# index-signing key from scripts/gen-update-keys.sh into one attached
# index.p7, then uploads the bundle FIRST - checking the copy - and the
# index LAST, so a board never sees an index naming a file that is not
# there, or not whole. Any failure stops it before the index is published.
# The published files are world-readable (0644, directories 0755) whatever
# the umask, so the web server can serve them.
#
# Needs OpenSSL 3 (Ubuntu 22.04 or newer) here; rsync of any version here
# and on the far side.
#
# BOAT_PUBLISH_DEST is a local directory (a web server's document root) or an
# rsync target (host:path); either way it is the directory that holds one
# subdirectory per channel. For object storage, publish to a local directory
# and sync that with your provider's tool - in the same order.
#
# Options:
#   --reflash      mark the release as a BSP bump: boards are told to
#                  reflash at the dock instead of downloading it. Also
#                  what a bundle built against another kernel device tree
#                  than the channel's current release needs (its boards
#                  would refuse it) - the script stops without it
#   --notes FILE   release notes shown by `boat-update status`
#
# Promote a release from testing to stable by publishing the same bundle to
# stable, once it has committed on the testing board: names are
# content-addressed, so the file is never renamed.
#
# A bundle from a BOAT_BENCH build (its compatible ends in -bench) goes to
# the 'bench' channel only, and nothing else does: its rootfs trusts the
# bench CA (docs/plans/ab-validation.md). The bench channel's index is
# signed with the bench keys (BOAT_BENCH_KEYS_DIR, which bench images
# trust), so a bench host needs no fleet private key at all.
set -euo pipefail
# Everything this writes - the index, the copies, the directories - is for a
# web server running as another user.
umask 022
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
# The deploy directory's boat-bundle-<MACHINE>.raucb is a symlink, and
# rsync without -L skips a symlink ("skipping non-regular file") and still
# exits 0 - which would publish an index naming a bundle that never
# arrived. Publish the file it points at.
BUNDLE="$(readlink -f -- "${BUNDLE}")"
: "${BOAT_PUBLISH_DEST:?set BOAT_PUBLISH_DEST to the channel root (a directory, or host:path for rsync)}"
need openssl
need python3
need flock
# -no-CAstore below - trust the update CA and nothing from the system - is
# OpenSSL 3's: an older openssl rejects the option, which would read as a
# bundle that does not verify.
[[ "$(openssl version | awk '{ print $2 }')" =~ ^[3-9]\. ]] \
  || die "publishing needs OpenSSL 3.0 or newer (cms -no-CAstore); this host has '$(openssl version)' - use Ubuntu 22.04 or newer"

# The keys this channel is published with: the bench's for 'bench', the
# fleet's for every other channel.
if [[ "${CHANNEL}" == bench ]]; then
  KEYS="${BOAT_BENCH_KEYS_DIR}"
  GEN="scripts/gen-update-keys.sh --bench"
else
  KEYS="${BOAT_KEYS_DIR}"
  GEN="scripts/gen-update-keys.sh"
fi
KEY="${KEYS}/index-signing.key.pem"
CERT="${KEYS}/index-signing.cert.pem"
[[ -f "${KEY}" && -f "${CERT}" && -f "${KEYS}/ca.cert.pem" ]] \
  || die "no index-signing key and CA in ${KEYS} - run ${GEN}"
# The same file is every image's keyring (rauc_%.bbappend refuses it too).
! grep -q 'PRIVATE KEY' "${KEYS}/ca.cert.pem" \
  || die "${KEYS}/ca.cert.pem holds a PRIVATE KEY - it must hold only CA certificates"

# One publish at a time per key directory: two at once would compute the
# same serial for different indexes, and boards take only the first.
exec 8>>"${KEYS}/index-serial.lock" || die "cannot open ${KEYS}/index-serial.lock"
flock -w 600 8 || die "another publish-update.sh holds ${KEYS}/index-serial.lock"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# The bundle, checked as a board's RAUC checks it - before any board spends
# its link on it. RAUC 1.13's verity format appends a CMS signature that
# carries the manifest, then the signature's size as 8 bytes, big-endian.
# A plain bundle (detached signature), one signed under another CA or by a
# key that is not a bundle-signing one, or no bundle at all, stops here.
# The index is made from the manifest inside the verified signature.
# Bench images trust both CAs, so a bench bundle may be signed under either.
BUNDLE_CA="${WORK}/bundle-ca.pem"
cat "${KEYS}/ca.cert.pem" > "${BUNDLE_CA}"
if [[ "${CHANNEL}" == bench && -f "${BOAT_KEYS_DIR}/ca.cert.pem" ]]; then
  cat "${BOAT_KEYS_DIR}/ca.cert.pem" >> "${BUNDLE_CA}"
fi
python3 - "${BUNDLE}" "${WORK}/bundle.sig" <<'PY' || die "${BUNDLE} is not a signed RAUC bundle (no signature at its end)"
import struct, sys
with open(sys.argv[1], "rb") as f:
    end = f.seek(0, 2)
    if end < 8:
        sys.exit(1)
    f.seek(end - 8)
    size = struct.unpack(">Q", f.read(8))[0]
    if not 0 < size <= min(64 * 1024, end - 8):
        sys.exit(1)
    f.seek(end - 8 - size)
    sig = f.read(size)
with open(sys.argv[2], "wb") as out:
    out.write(sig)
PY
openssl cms -verify -inform DER -binary -in "${WORK}/bundle.sig" -out "${WORK}/manifest.raucm" \
  -signer "${WORK}/bundle-signer.pem" -CAfile "${BUNDLE_CA}" -no-CApath -no-CAstore -purpose any \
  2> "${WORK}/verify.err" \
  || die "${BUNDLE}'s signature does not verify against ${KEYS}/ca.cert.pem, or it is not a verity bundle. openssl said: $(head -c 2000 "${WORK}/verify.err" | tr '\n' ' ')"
openssl x509 -in "${WORK}/bundle-signer.pem" -noout -ext extendedKeyUsage 2>/dev/null | grep -q "Code Signing" \
  || die "${BUNDLE} is signed by a certificate without the codeSigning extended key usage - not the bundle-signing key"

SHA256="$(sha256sum "${BUNDLE}" | cut -d' ' -f1)"
SIZE="$(stat -c %s "${BUNDLE}")"

# The index this channel publishes now, if it can be read back (a local
# directory, or over rsync): its serial is a lower bound for the next one,
# and its release says what the boards on this channel were last offered.
PUBLISHED_SERIAL=0
PUBLISHED_FIRMWARE=""
PUBLISHED_VERSION=""
if [[ "${BOAT_PUBLISH_DEST}" == *:* ]]; then
  need rsync
  rsync -q "${BOAT_PUBLISH_DEST}/${CHANNEL}/index.p7" "${WORK}/published.p7" 2>/dev/null || true
elif [[ -f "${BOAT_PUBLISH_DEST}/${CHANNEL}/index.p7" ]]; then
  cp "${BOAT_PUBLISH_DEST}/${CHANNEL}/index.p7" "${WORK}/published.p7"
fi
if [[ -s "${WORK}/published.p7" ]] \
   && openssl cms -verify -inform DER -binary -in "${WORK}/published.p7" -out "${WORK}/published.json" \
        -CAfile "${KEYS}/ca.cert.pem" -no-CApath -no-CAstore -purpose smimesign -no_check_time 2>/dev/null; then
  read -r PUBLISHED_SERIAL PUBLISHED_VERSION PUBLISHED_FIRMWARE < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(int(d.get("serial", 0)), d.get("version", "?") or "?", d.get("firmware", ""))' "${WORK}/published.json") \
    || PUBLISHED_SERIAL=0
fi

# The serial only ever grows: a board rejects an index whose serial is not
# above the last one it accepted on that channel. It is the largest of the
# last serial here + 1, the channel's current index's + 1 and the time in
# seconds, so a lost or restored counter, or a second publishing machine
# (even one whose clock is behind), can still never go back below what
# boards have seen - as long as the channel's index can be read back. A
# counter that is there but unreadable stops the publish.
SERIAL_FILE="${KEYS}/index-serial"
LAST=0
if [[ -e "${SERIAL_FILE}" ]]; then
  LAST="$(tr -d '[:space:]' < "${SERIAL_FILE}")"
  [[ "${LAST}" =~ ^[0-9]+$ ]] \
    || die "${SERIAL_FILE} holds '${LAST}', not a number - fix it first: boards refuse an index whose serial does not grow"
fi
(( PUBLISHED_SERIAL <= LAST )) || LAST="${PUBLISHED_SERIAL}"
NOW="$(date +%s)"
SERIAL=$(( LAST + 1 > NOW ? LAST + 1 : NOW ))

python3 - "${WORK}/manifest.raucm" "${WORK}/index.json" "${CHANNEL}" "${SERIAL}" \
         "${SIZE}" "${SHA256}" "${REFLASH}" "${NOTES}" <<'PY'
import configparser, datetime, json, sys
manifest, out, channel, serial, size, sha, reflash, notes = sys.argv[1:]
cp = configparser.ConfigParser(interpolation=None, strict=False)
cp.read(manifest)
if cp.get("bundle", "format", fallback="plain") != "verity":
    sys.exit("the bundle is not in RAUC's verity format, which boards require (bundle-formats=-plain)")
version = cp.get("update", "version")
doc = {
    "channel": channel,
    "serial": int(serial),
    "compatible": cp.get("update", "compatible"),
    "version": version,
    "l4t": cp.get("meta.boat", "l4t", fallback=""),
    "firmware": cp.get("meta.boat", "firmware", fallback=""),
    "bundle": "boat-bundle-%s-%s.raucb" % (version, sha[:12]),
    "size": int(size),
    "sha256": sha,
    "published": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "requires_reflash": reflash == "true",
}
if not doc["l4t"]:
    sys.exit("the bundle has no [meta.boat] l4t= - it was not built by meta-boat's boat-bundle")
if not doc["firmware"]:
    del doc["firmware"]
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
read -r VERSION NAME FIRMWARE < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["version"], d["bundle"], d.get("firmware", ""))' "${WORK}/index.json")

# A slot carries no device tree: the kernel runs on the one written at
# flash. Boards flashed for the channel's current release refuse a bundle
# built against another DTB (their pre-install handler) - after each has
# downloaded it. Such a release is a reflash.
if [[ -n "${FIRMWARE}" && -n "${PUBLISHED_FIRMWARE}" && "${FIRMWARE}" != "${PUBLISHED_FIRMWARE}" ]] \
   && [[ "${REFLASH}" != true ]]; then
  die "${VERSION} was built against another kernel device tree than ${PUBLISHED_VERSION}, which '${CHANNEL}' publishes now (firmware.id ${FIRMWARE:0:12}, not ${PUBLISHED_FIRMWARE:0:12}): boards keep the DTB they were flashed with and would refuse it. Publish it with --reflash (boards are told to reflash at the dock), or rebuild against the same meta-tegra"
fi

openssl cms -sign -nodetach -binary -outform DER \
  -signer "${CERT}" -inkey "${KEY}" -in "${WORK}/index.json" -out "${WORK}/index.p7"
chmod 0644 "${WORK}/index.p7"
# Check it the way a board will, before anything is uploaded.
openssl cms -verify -inform DER -binary -in "${WORK}/index.p7" -out /dev/null \
  -CAfile "${KEYS}/ca.cert.pem" -no-CApath -no-CAstore -purpose smimesign -no_check_time 2>/dev/null \
  || die "the signed index does not verify against ${KEYS}/ca.cert.pem"

# Record the serial before anything is uploaded, atomically: a publish that
# fails half way must not be followed by one that reuses its serial. Each
# step its own command: under set -e, a failure inside an && list goes on.
printf '%s\n' "${SERIAL}" > "${SERIAL_FILE}.tmp" || die "cannot write ${SERIAL_FILE}"
mv "${SERIAL_FILE}.tmp" "${SERIAL_FILE}" || die "cannot write ${SERIAL_FILE}"

log "publishing ${VERSION} to '${CHANNEL}' as ${NAME} (serial ${SERIAL}, $(( SIZE >> 20 )) MiB)"
if [[ "${BOAT_PUBLISH_DEST}" == *:* ]]; then
  # The channel directory first, the way any rsync can (--mkpath is
  # 3.2.3's, on both ends): rsync makes a missing destination directory,
  # and copying an empty directory makes the channel's below it.
  mkdir -p "${WORK}/tree/${CHANNEL}"
  rsync -r --perms --chmod=D0755 "${WORK}/tree/${CHANNEL}" "${BOAT_PUBLISH_DEST}/"
  # rsync to a temporary name and rename on the far side is what makes the
  # index appear in one step; --partial-dir does that for us. --perms with
  # --chmod: the far side's umask does not decide who can read them.
  rsync --perms --chmod=D0755,F0644 --partial-dir=.rsync-partial "${BUNDLE}" "${BOAT_PUBLISH_DEST}/${CHANNEL}/${NAME}"
  rsync --perms --chmod=D0755,F0644 --partial-dir=.rsync-partial "${WORK}/index.p7" "${BOAT_PUBLISH_DEST}/${CHANNEL}/index.p7"
else
  DEST="${BOAT_PUBLISH_DEST}/${CHANNEL}"
  mkdir -p "${DEST}"
  # Each step its own command, so any failure stops the publish (set -e
  # does not stop at a failure inside an && list) - and the copy is checked
  # before an index names it: a full disk can leave a short file.
  cp "${BUNDLE}" "${DEST}/.${NAME}.tmp" || die "could not copy the bundle to ${DEST} - nothing published"
  chmod 0644 "${DEST}/.${NAME}.tmp"
  [[ "$(stat -c %s "${DEST}/.${NAME}.tmp")" == "${SIZE}" \
     && "$(sha256sum "${DEST}/.${NAME}.tmp" | cut -d' ' -f1)" == "${SHA256}" ]] \
    || die "the copy in ${DEST} is not the bundle (short or changed) - nothing published"
  mv "${DEST}/.${NAME}.tmp" "${DEST}/${NAME}" || die "could not move the bundle into place - nothing published"
  cp "${WORK}/index.p7" "${DEST}/.index.p7.tmp" || die "could not copy the index to ${DEST} - the bundle is there, the old index still in place"
  mv "${DEST}/.index.p7.tmp" "${DEST}/index.p7" || die "could not move the index into place"
fi
log "published. Boards following '${CHANNEL}' see it at their next check ('boat-update check' to ask now)."
