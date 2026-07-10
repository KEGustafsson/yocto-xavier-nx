# elfutils_0.186.bb already works around one newer-GCC false positive
# (BUILD_CFLAGS += "-Wno-error=stringop-overflow"). libcpu/riscv_disasm.c
# also trips "-Werror=discarded-qualifiers" on GCC 12 (a bsearch() result
# assignment) that didn't fire on the GCC the recipe was written against.
# This is elfutils-native, a host-side build tool only - it doesn't affect
# the target image - so silencing the one warning is safe, matching the
# recipe's own existing precedent rather than patching upstream source.
BUILD_CFLAGS += "-Wno-error=discarded-qualifiers"
