SUMMARY = "BENCH ONLY: make an A/B trial boot fail in a chosen way"
DESCRIPTION = "Installed only in boat-image-faulty, for proving on the bench \
that the A/B fallback catches each kind of failure (docs/plans/\
ab-validation.md). BOAT_FAULT picks the kind: panic, emergency, tier2 or \
hang. It acts only on a trial boot. Never build it into an image for a \
boat: the recipe refuses unless BOAT_BENCH = \"1\"."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://boat-fault.sh file://boat-fault.service"
S = "${WORKDIR}"

inherit allarch systemd

BOAT_FAULT ??= ""
BOAT_FAULT_KINDS = "panic emergency tier2 hang"

python () {
    if d.getVar('BOAT_BENCH') != '1':
        raise bb.parse.SkipRecipe("bench only: needs BOAT_BENCH = \"1\"")
    if d.getVar('BOAT_FAULT') not in d.getVar('BOAT_FAULT_KINDS').split():
        raise bb.parse.SkipRecipe("set BOAT_FAULT to one of: %s" % d.getVar('BOAT_FAULT_KINDS'))
}

do_configure[noexec] = "1"
do_compile[noexec] = "1"

do_install() {
    install -d ${D}${sbindir} ${D}${systemd_system_unitdir}
    sed -e 's|@FAULT@|${BOAT_FAULT}|g' ${WORKDIR}/boat-fault.sh > ${D}${sbindir}/boat-fault
    chmod 0755 ${D}${sbindir}/boat-fault
    install -m 0644 ${WORKDIR}/boat-fault.service ${D}${systemd_system_unitdir}/
}
do_install[vardeps] += "BOAT_FAULT"

SYSTEMD_SERVICE:${PN} = "boat-fault.service"
FILES:${PN} = "${sbindir}/boat-fault ${systemd_system_unitdir}"
