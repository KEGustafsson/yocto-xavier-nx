FILESEXTRAPATHS:prepend := "${THISDIR}/files:"

# DeviceTree.inf preprocesses .dts files with "cpp -x assembler-with-cpp
# -undef", a mode where GCC's preprocessor can't parse
# __has_feature(...)/__has_builtin(...) as callable operators even
# though defined(__has_feature) reports true - MdePkg/Include/Base.h's
# clang-compat fallbacks (dead code for our GCC-only build) hit that
# and fail do_compile with "missing binary operator before token '('".
SRC_URI += "file://0001-Base.h-guard-has_feature-has_builtin-for-assembler-.patch"
