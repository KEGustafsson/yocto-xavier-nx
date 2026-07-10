FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# pseudo's bundled openat2 wrapper is just a stub returning ENOSYS. That
# breaks do_compile on hosts with glibc >= 2.39 (const-qualifier
# mismatch) and breaks do_package/do_rootfs at runtime on hosts whose
# tar actually calls openat2() (e.g. Ubuntu 26.04's CVE-2025-45582 fix)
# and treats ENOSYS as fatal instead of retrying with plain openat().
SRC_URI += "file://0001-openat2-implement-wrapper.patch"
