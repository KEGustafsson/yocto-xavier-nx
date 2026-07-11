# 02 — Host Prerequisites

## Build/flash host

- **x86-64 Linux, running natively.** Ubuntu 20.04 or 22.04 are the best-tested
  hosts for kirkstone + meta-tegra. Do **not** build or flash from a VM, WSL, an
  ARM machine, or macOS — flashing needs raw USB and NVIDIA's x86-64 binaries.
- **Newer hosts (Ubuntu 24.04+, 26.04, …) also work**, but kirkstone (Yocto 4.0,
  ~2022) predates their toolchains and the build scripts compensate for it:
  - `scripts/02-configure-build.sh` points `-native` builds at `gcc-12`/`g++-12`
    instead of the system compiler, because newer GCC defaults (C23) break old
    gnulib/K&R code bundled in several `-native` recipes. **Install these
    yourself first** — the dep script doesn't, since only very new hosts need
    them: `sudo apt-get install gcc-12 g++-12`.
  - `scripts/pyfix/sitecustomize.py` patches around Python-version removals
    (e.g. `ast.Str`, gone since 3.12) and a `multiprocessing` default-method
    change (3.14) that bitbake itself hits on hosts this new.
  - A `meta-boat` patch works around a glibc/GCC-version mismatch in
    `pseudo-native`'s `openat2` wrapper.

  None of this needs manual action beyond installing `gcc-12`/`g++-12` above —
  it's automatic in the scripts — but if you hit a build failure that looks
  like a compiler/Python-version issue rather than a real code bug, this is
  why, and the fix likely already exists in `scripts/`.
- **Disk:** ~**150 GB** free for the first build (downloads + sstate + tmp).
  `scripts/02-configure-build.sh` enables bitbake's `rm_work`, which deletes
  each recipe's `tmp/work/` right after it builds, so usage stays close to
  that instead of growing to 50GB+ over a full image build.
- **RAM:** 16 GB minimum, 32 GB+ recommended. **CPU:** the more cores the better;
  a first build is 2–6 h.
- **Time/network:** the first build downloads many GB of source.

## Hardware you need

- Jetson **Xavier NX Developer Kit**.
- An **NVMe M.2 SSD** (2280) installed in the carrier board's M.2 Key-M slot.
- **USB-C cable** from the devkit's OTG port to the host (for recovery-mode flashing).
- **DC barrel jack power supply** (flash with the jack, not USB power; set the
  power-select jumper for barrel-jack).
- A **USB-TTL serial adapter** on the devkit's UART header is strongly
  recommended so you can watch the boot console.
- An SD card is **optional** (only as a fallback, per `06-troubleshooting.md`).

## Install host packages

```bash
./scripts/00-install-host-deps.sh
```

This installs the Yocto "Required Packages for the Build Host" plus the tools
`initrd-flash` needs (`usbutils`, `zstd`, `libxml2-utils`, …). It also warns you
if **TLP** is installed — TLP can cut USB power mid-flash; remove it and reboot
before flashing if present.

## Two gotchas that cost people hours

1. **Never run `bitbake` as root.** Use a normal user. The scripts refuse to run
   the build as root.
2. **Locale must be UTF-8.** The dep script runs `locale-gen en_US.UTF-8`; if you
   see `Please use a locale setting which supports UTF-8`, fix your locale.

Next: [`03-phase1-first-bootable-nvme.md`](03-phase1-first-bootable-nvme.md)
