SUMMARY = "BENCH ONLY: boat-image whose trial boots fail on purpose"
DESCRIPTION = "boat-image plus boat-fault: the same image, except that a \
trial boot of it fails in the way BOAT_FAULT names. Bundled as \
boat-bundle-faulty and signed by the bench CA, which only BOAT_BENCH \
images trust, it is how the A/B fallback is proven on the bench \
(docs/plans/ab-validation.md)."

require recipes-core/images/boat-image.bb

IMAGE_INSTALL:append = " boat-fault"

python () {
    if d.getVar('BOAT_BENCH') != '1':
        raise bb.parse.SkipRecipe("bench only: needs BOAT_BENCH = \"1\"")
}
