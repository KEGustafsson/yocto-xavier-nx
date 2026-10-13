SUMMARY = "BENCH ONLY: a bundle of boat-image-faulty, signed by the bench CA"
DESCRIPTION = "Installs like any update, then fails its trial in the way \
BOAT_FAULT names - the bench's proof that the A/B fallback catches it \
(docs/plans/ab-validation.md). Signed by the bench CA \
(scripts/gen-update-keys.sh --bench), which only BOAT_BENCH images trust: a \
boat's image rejects it before writing anything."

require boat-bundle.bb

BOAT_BUNDLE_IMAGE = "boat-image-faulty"
BUNDLE_BASENAME = "boat-bundle-faulty-${BOAT_FAULT}"

RAUC_KEY_FILE = "${BOAT_BENCH_KEYS_DIR}/rauc-signing.key.pem"
RAUC_CERT_FILE = "${BOAT_BENCH_KEYS_DIR}/rauc-signing.cert.pem"
RAUC_KEYRING_FILE = "${BOAT_BENCH_KEYS_DIR}/ca.cert.pem"

python () {
    if d.getVar('BOAT_BENCH') != '1':
        raise bb.parse.SkipRecipe("bench only: needs BOAT_BENCH = \"1\"")
    if not d.getVar('BOAT_FAULT'):
        raise bb.parse.SkipRecipe("set BOAT_FAULT (see boat-fault.bb)")
}
