# Drop OpenCV's bundled example programs from the image.
#
# The default PACKAGECONFIG in meta-oe's opencv recipe includes "samples",
# which turns on -DBUILD_EXAMPLES=ON/-DINSTALL_PYTHON_EXAMPLES=ON and creates
# an opencv-samples package holding ${datadir}/opencv4/samples/. That matters
# here because `opencv` is a metapackage whose RDEPENDS is *generated* from
# every non-dev/dbg/doc runtime sub-package (see populate_packages:prepend in
# the recipe) - so naming `opencv` in packagegroup-boat-cuda would drag the
# samples in whether or not anyone wants them, and this image is flashed into
# a deliberately small rootfs (ROOTFS_SIZE_BYTES, 16 GiB - see scripts/env.sh)
# where every byte crosses a USB 2.0 link.
#
# Removing the PACKAGECONFIG rather than just excluding the package also skips
# *building* the examples, which is the larger saving on a CUDA-enabled
# OpenCV: the samples compile against the CUDA modules too.
#
# ":remove" is applied last during expansion, after meta-tegra's own
# "PACKAGECONFIG:append:cuda = ${OPENCV_CUDA_SUPPORT}" - so the cuda and dnn
# entries that give this board its GPU-accelerated OpenCV are untouched.
PACKAGECONFIG:remove = "samples"
