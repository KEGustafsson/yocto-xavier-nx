#!/usr/bin/env bash
# Create the keys that sign A/B update bundles - once, ever, per fleet.
#
#   scripts/gen-update-keys.sh                 # into BOAT_KEYS_DIR (scripts/env.sh)
#   BOAT_KEYS_DIR=/secure/place scripts/gen-update-keys.sh
#
# Writes, all EC P-256, into BOAT_KEYS_DIR (default
# ~/.local/share/boat-update-keys):
#
#   ca.key.pem / ca.cert.pem       the update CA. Every image trusts exactly
#                                  this certificate (/etc/rauc/ca.cert.pem).
#                                  Its key signs the two below and nothing
#                                  else: keep it OFFLINE once they exist.
#   rauc-signing.{key,cert}.pem    signs RAUC bundles (codeSigning EKU;
#                                  system.conf: check-purpose=codesign)
#   index-signing.{key,cert}.pem   signs the OTA channel index (Phase 5;
#                                  emailProtection EKU, verified with
#                                  openssl cms -purpose smimesign)
#
# LOSE ca.key.pem AND ca.cert.pem AND EVERY BOARD NEEDS A USB REFLASH to
# trust a new CA. Back the directory up somewhere that is not this machine.
# The script refuses to overwrite anything, writes private keys mode 600,
# and refuses a directory inside the repository or WORKROOT (yocto/ gets
# deleted; a key in the repository gets committed).
#
# See docs/plans/ab-os-updates.md section 5.7.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

case "${1:-}" in
  -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
  "") ;;
  *) die "unexpected argument '$1' (see --help)" ;;
esac

need openssl
DIR="${BOAT_KEYS_DIR}"
[[ "${DIR}" == /* ]] || die "BOAT_KEYS_DIR must be an absolute path, got '${DIR}'"
case "${DIR}/" in
  "${REPO_ROOT}/"*|"${WORKROOT}/"*)
    die "BOAT_KEYS_DIR (${DIR}) is inside the repository or WORKROOT - pick a place a clean-up cannot delete and git cannot commit" ;;
esac

FILES=(ca.key.pem ca.cert.pem rauc-signing.key.pem rauc-signing.cert.pem
       index-signing.key.pem index-signing.cert.pem)
for f in "${FILES[@]}"; do
  [[ ! -e "${DIR}/${f}" ]] || die "${DIR}/${f} already exists - not overwriting update keys. Move the directory aside first if you really mean to start a new CA (every board then needs a USB reflash)."
done

umask 077
mkdir -p "${DIR}"
chmod 700 "${DIR}"
WORK="$(mktemp -d "${DIR}/.gen.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

# Twenty years: the board has no RTC and RAUC checks certificates at the
# bundle's signing time (use-bundle-signing-time), so expiry is not what
# protects anything here - revocation is removing the CA (plan 5.7).
DAYS=7300

newkey() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$1"; }

log "Creating the update CA ..."
newkey "${WORK}/ca.key.pem"
openssl req -new -x509 -key "${WORK}/ca.key.pem" -out "${WORK}/ca.cert.pem" \
  -days "${DAYS}" -subj "/O=boat/CN=boat update CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash"

# leaf NAME CN EKU
leaf() {
  local name="$1" cn="$2" eku="$3"
  log "Creating ${name} (${eku}) ..."
  newkey "${WORK}/${name}.key.pem"
  openssl req -new -key "${WORK}/${name}.key.pem" -out "${WORK}/${name}.csr" \
    -subj "/O=boat/CN=${cn}"
  cat > "${WORK}/${name}.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=${eku}
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EOF
  openssl x509 -req -in "${WORK}/${name}.csr" -out "${WORK}/${name}.cert.pem" \
    -CA "${WORK}/ca.cert.pem" -CAkey "${WORK}/ca.key.pem" -CAcreateserial \
    -days "${DAYS}" -extfile "${WORK}/${name}.ext"
  openssl verify -CAfile "${WORK}/ca.cert.pem" "${WORK}/${name}.cert.pem" >/dev/null \
    || die "${name}.cert.pem does not verify against the new CA"
}
leaf rauc-signing "boat update bundle signing" codeSigning
leaf index-signing "boat update channel index signing" emailProtection

for f in "${FILES[@]}"; do
  case "${f}" in
    *.key.pem)  install -m 600 "${WORK}/${f}" "${DIR}/${f}" ;;
    *.cert.pem) install -m 644 "${WORK}/${f}" "${DIR}/${f}" ;;
  esac
done

log "Update keys written to ${DIR}:"
ls -l "${DIR}" | sed 's/^/    /'
warn "BACK THIS DIRECTORY UP NOW, somewhere other than this machine."
warn "Without ca.key.pem and ca.cert.pem, every board needs a USB reflash to trust a new CA."
warn "Once the backup exists, ca.key.pem is only needed to issue new signing certificates: keep it offline."
