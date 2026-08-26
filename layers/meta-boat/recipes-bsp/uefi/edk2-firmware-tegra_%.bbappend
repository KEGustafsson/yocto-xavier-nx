FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# "_%" rather than the pinned "_35.6.4" this used to carry. scripts/01-fetch-layers.sh
# does `git reset --hard origin/kirkstone` on every run, so the moment OE4T
# bumps L4T the versioned recipe is renamed and a version-pinned bbappend
# becomes a DANGLING append - which bitbake reports as a hard parse ERROR, not
# a warning, and which then blocks every target rather than just this one. The
# patch below guards preprocessor-only code and is not version-specific, so
# there is nothing for the pin to protect.

# DeviceTree.inf preprocesses .dts files with "cpp -x assembler-with-cpp
# -undef", a mode where GCC's preprocessor can't parse
# __has_feature(...)/__has_builtin(...) as callable operators even
# though defined(__has_feature) reports true - MdePkg/Include/Base.h's
# clang-compat fallbacks (dead code for our GCC-only build) hit that
# and fail do_compile with "missing binary operator before token '('".
SRC_URI += "file://0001-Base.h-guard-has_feature-has_builtin-for-assembler-.patch"
