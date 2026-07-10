FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# Host glibc >= 2.39 added a const qualifier to openat2()'s open_how
# parameter; pseudo's bundled wrapper predates that and fails do_compile
# with "conflicting types for 'openat2'" on newer distros (e.g. this
# build's Ubuntu 26.04 / glibc 2.43).
SRC_URI += "file://0001-openat2-const-open_how.patch"
