SUMMARY = "Signed RAUC update bundle of boat-image for the A/B layout"
DESCRIPTION = "A verity-format RAUC bundle carrying boat-image's rootfs, \
installed with 'rauc install' (and, from Phase 5, boat-update). Its version \
is BOAT_RELEASE, the same string the image writes to /etc/boat-release; its \
[meta.boat] section names the L4T release and Docker version the image was \
built with, for the board's pre-install checks; its hook carries the \
board's own state into the new slot. Every bundle is stream-ready \
(adaptive block-hash-index). See docs/plans/ab-os-updates.md 5.6-5.10."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

inherit bundle

SRC_URI = "file://boat-bundle-hook.sh"

# The image this bundle updates the rootfs slot with.
BOAT_BUNDLE_IMAGE ?= "boat-image"

RAUC_BUNDLE_COMPATIBLE = "${BOAT_BUNDLE_COMPATIBLE}"
RAUC_BUNDLE_VERSION = "${BOAT_RELEASE_EFFECTIVE}"
RAUC_BUNDLE_DESCRIPTION = "${BOAT_BUNDLE_IMAGE} ${BOAT_RELEASE_EFFECTIVE} for ${MACHINE}"
RAUC_BUNDLE_FORMAT = "verity"

RAUC_BUNDLE_SLOTS = "rootfs"
RAUC_SLOT_rootfs = "${BOAT_BUNDLE_IMAGE}"
RAUC_SLOT_rootfs[fstype] = "ext4"
RAUC_SLOT_rootfs[hooks] = "post-install"
# Lets a board stream only the blocks it does not already have (Phase 6);
# a manifest flag, so every bundle from now on can be streamed.
RAUC_SLOT_rootfs[adaptive] = "block-hash-index"

RAUC_BUNDLE_HOOKS[file] = "boat-bundle-hook.sh"

# Filled in from the image itself by boat_bundle_meta, below.
RAUC_META_SECTIONS = "boat"

RAUC_KEY_FILE = "${BOAT_KEYS_DIR}/rauc-signing.key.pem"
RAUC_CERT_FILE = "${BOAT_KEYS_DIR}/rauc-signing.cert.pem"
RAUC_KEYRING_FILE = "${BOAT_KEYS_DIR}/ca.cert.pem"
# Their contents, not just their paths, belong in the signature: a renewed
# signing certificate at the same path must produce a newly signed bundle.
do_bundle[file-checksums] += "${RAUC_KEY_FILE}:True ${RAUC_CERT_FILE}:True ${RAUC_KEYRING_FILE}:True"

DEPENDS += "e2fsprogs-native"

python boat_check_bundle_keys() {
    import os
    for var in ('RAUC_KEY_FILE', 'RAUC_CERT_FILE', 'RAUC_KEYRING_FILE'):
        f = d.getVar(var)
        if not f or not os.path.isfile(f):
            bb.fatal("%s needs the update signing keys: %s (%r) does not exist. Run "
                     "scripts/gen-update-keys.sh once (--bench for the bench keys), keep the "
                     "directory safe, and point BOAT_KEYS_DIR (BOAT_BENCH_KEYS_DIR) at it "
                     "(scripts/env.sh)." % (d.getVar('PN'), var, f))
}

# Read what the bundle says about itself from the image it carries, so it
# cannot disagree with it: the release in /etc/boat-release must be this
# bundle's version, the L4T release comes from /etc/nv_tegra_release, the
# Docker version from the image manifest - and the kernel and initrd GRUB
# boots must be in /boot.
python boat_bundle_meta() {
    import os
    import re
    import subprocess

    image = d.expand("${DEPLOY_DIR_IMAGE}/${BOAT_BUNDLE_IMAGE}-${MACHINE}.ext4")
    # e2fsprogs-native puts debugfs in ${base_sbindir}, not ${sbindir}:
    # look it up the way the task's own commands would.
    debugfs = bb.utils.which(d.getVar('PATH'), 'debugfs')
    if not debugfs:
        bb.fatal("debugfs is not in the native sysroot (DEPENDS on e2fsprogs-native)")

    def debugfs_run(request):
        r = subprocess.run([debugfs, "-R", request, image],
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    for path in ("/boot/Image", "/boot/initrd"):
        rc, out, err = debugfs_run("stat %s" % path)
        if rc != 0 or "Inode:" not in out:
            bb.fatal("%s has no %s - GRUB would have nothing to boot in that slot "
                     "(UBOOT_EXTLINUX = \"1\" puts it there)" % (image, path))

    rc, release, err = debugfs_run("cat /etc/boat-release")
    release = release.strip()
    if release != d.getVar('RAUC_BUNDLE_VERSION'):
        bb.fatal("%s says it is release %r, but this bundle would be %r - rebuild the "
                 "image with the same BOAT_RELEASE" % (image, release, d.getVar('RAUC_BUNDLE_VERSION')))
    # RAUC hands its system handlers the [meta.*] keys but not the manifest's
    # version, so the handlers read the release from here.
    d.setVarFlag('RAUC_META_boat', 'release', release)

    rc, nv, err = debugfs_run("cat /etc/nv_tegra_release")
    m = re.search(r'^# R(\d+) \(release\), REVISION: (\d+)\.(\d+)', nv)
    if not m:
        bb.fatal("cannot read the L4T release from %s:/etc/nv_tegra_release" % image)
    d.setVarFlag('RAUC_META_boat', 'l4t', '%s.%s.%s' % m.groups())

    docker = ''
    manifest = d.expand("${DEPLOY_DIR_IMAGE}/${BOAT_BUNDLE_IMAGE}-${MACHINE}.manifest")
    if os.path.exists(manifest):
        with open(manifest) as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 3 and fields[0] in ('docker-ce', 'docker-moby', 'docker'):
                    docker = fields[2]
                    break
    d.setVarFlag('RAUC_META_boat', 'docker', docker)
}
do_configure[prefuncs] += "boat_check_bundle_keys boat_bundle_meta"
do_configure[vardeps] += "BOAT_RELEASE_EFFECTIVE"
