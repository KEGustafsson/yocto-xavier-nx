#!/usr/bin/env python3
"""Run boat-ab-layout.py, as the build does, on a template in NVIDIA's shape.

    scripts/test-ab-layout.py

boat-ab-layout.py turns NVIDIA's flash_l4t_nvme_rootfs_ab.xml into the
boat's NVMe layout: APP_b left unwritten at flash time, SWAP and a
fill-to-end DATA appended, and the GPT entry number of every partition the
boot and update path addresses pinned. Those numbers are what GRUB, the
initramfs and RAUC address the slots by, so a layout that numbers them
differently boots - and writes - the wrong partition.

The build only runs it inside bitbake against NVIDIA's BSP, which is not in
this repository. This runs it on two templates: NVIDIA's own R35.6.4
flash_l4t_nvme_rootfs_ab.xml partition list, in its order (kernel id="3"
first, the partitions after it numbered by position, APP id="1" and APP_b
id="2" last, just before the secondary GPT), and a synthetic one numbered
purely by position (APP and APP_b first, no ids) - so the pinned numbers are
checked whether they come from ids or from places. It checks the result:
numbers, names, contents and fill-to-end, also after the flasher's own
`sed '/DATAFILE/d'`, and that each template it must not accept is refused.
When meta-tegra is checked out (yocto/layers, or META_TEGRA=<dir>), the
result is also read back through meta-tegra's own nvflashxmlparse, the
parser the flash itself uses; CI has no meta-tegra, so there the numbering
is this file's model of that parser (numbered(), below).

It also runs the host scripts' guards that tie a build and a flash to the
A/B layout they were configured for: 03-build.sh refuses a release, floor
or key directory that local.conf does not have, and 05-flash-nvme.sh an
unpack whose layout is not the configured build's.

Exit status: 0 = all passed, 1 = a case failed.
"""
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "layers", "meta-boat", "recipes-bsp",
                      "tegra-binaries", "files", "boat-ab-layout.py")
META_TEGRA = os.environ.get("META_TEGRA") or os.path.join(
    HERE, "..", "yocto", "layers", "meta-tegra")
NVFLASHXMLPARSE = os.path.join(META_TEGRA, "recipes-bsp", "tegra-binaries",
                               "tegra-helper-scripts", "nvflashxmlparse.py")

NUMBERS = "APP=1,APP_b=2,esp=12,SWAP=17,DATA=18"
SWAP_BYTES = str(8 << 30)
DATA_MIN = str(4 << 30)

# NVIDIA's flash_l4t_nvme_rootfs_ab.xml (R35.6.4), in its order:
# (name, filename, id, type).
NVIDIA_PARTS = [
    ("kernel", "LNXFILE", "3", "kernel"), ("kernel-dtb", "DTB_FILE", None, "kernel_dtb"),
    ("reserved_for_chain_A_user", None, None, "data"), ("kernel_b", "LNXFILE_b", None, "kernel"),
    ("kernel-dtb_b", "DTB_FILE", None, "kernel_dtb"), ("reserved_for_chain_B_user", None, None, "data"),
    ("RECNAME", "RECFILE", None, "kernel"), ("RECDTB-NAME", "RECDTB-FILE", None, "kernel_dtb"),
    ("RECROOTFS", None, None, "data"), ("esp", "ESP_FILE", None, "data"),
    ("RECNAME_alt", None, None, "kernel"), ("RECDTB-NAME_alt", None, None, "kernel_dtb"),
    ("esp_alt", None, None, "data"), ("UDA", None, None, "data"),
    ("APP", "APPFILE", "1", "data"), ("APP_b", "APPFILE_b", "2", "data"),
]

# A synthetic layout numbered purely by position: the slots first, then
# kernels, recovery and the ESPs (no id attributes).
TEMPLATE_PARTS = [
    ("APP", "APPFILE"), ("APP_b", "APPFILE_b"), ("A_kernel", "LNXFILE"),
    ("A_kernel-dtb", "DTB_FILE"), ("A_reserved_on_user", None),
    ("B_kernel", "LNXFILE"), ("B_kernel-dtb", "DTB_FILE"),
    ("B_reserved_on_user", None), ("recovery", "RECFILE"),
    ("recovery-dtb", "RECDTB-FILE"), ("RECROOTFS", None), ("esp", "EFIFILE"),
    ("recovery_alt", None), ("recovery-dtb_alt", None), ("esp_alt", None),
    ("UDA_reserved", None),
]


def template(parts=TEMPLATE_PARTS, extra=None, alloc=None):
    """NVIDIA-shaped XML. extra: (name, id) of one more partition. alloc:
    {name: allocation_attribute}."""
    def part(name, ptype, filename=None, pid=None):
        attrs = ' id="%s"' % pid if pid else ""
        body = "".join("            <%s> %s </%s>\n" % (t, v, t) for t, v in (
            ("allocation_policy", "sequential"), ("filesystem_type", "basic"),
            ("size", "1048576"), ("file_system_attribute", "0"),
            ("allocation_attribute", (alloc or {}).get(name, "0x8")),
            ("align_boundary", "16384"), ("percent_reserved", "0"),
            ("filename", filename)) if v is not None)
        return '        <partition name="%s" type="%s"%s>\n%s        </partition>\n' % (
            name, ptype, attrs, body)
    xml = ('<?xml version="1.0"?>\n<partition_layout version="01.00.0000">\n'
           '    <device type="external" instance="0" sector_size="512" num_sectors="0">\n')
    xml += part("master_boot_record", "protective_master_boot_record")
    xml += part("primary_gpt", "primary_gpt")
    for entry in parts:
        name, filename, pid, ptype = (tuple(entry) + (None, "data"))[:4]
        xml += part(name, ptype, filename, pid)
    if extra:
        xml += part(extra[0], "data", pid=extra[1])
    xml += part("secondary_gpt", "secondary_gpt")
    return xml + "    </device>\n</partition_layout>\n"


def run_layout(workdir, xml, numbers=NUMBERS):
    src = os.path.join(workdir, "template.xml")
    dst = os.path.join(workdir, "boat-nvme-ab.xml")
    with open(src, "w") as f:
        f.write(xml)
    p = subprocess.run([sys.executable, SCRIPT, src, dst, SWAP_BYTES, DATA_MIN, numbers],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, dst


def numbered(path):
    """{name: (number, filename, fill-to-end)}, numbered the way meta-tegra's
    nvflashxmlparse does: an explicit id, else the previous number + 1."""
    out, nextpart = {}, 1
    for p in ET.parse(path).getroot().iter("partition"):
        if p.get("type") in ("protective_master_boot_record", "primary_gpt",
                             "secondary_gpt", "GPT", "GP1"):
            continue
        num = int(p.get("id", nextpart))
        nextpart = num + 1
        alloc = (p.findtext("allocation_attribute") or "0").strip()
        out[p.get("name")] = (num, (p.findtext("filename") or "").strip(),
                              bool(int(alloc, 16) & 0x800))
    return out


def expect_boat_layout(parts, errors, datafile="DATAFILE", esp_file="EFIFILE"):
    want = {"APP": (1, "APPFILE", False), "APP_b": (2, "", False),
            "esp": (12, esp_file, False), "SWAP": (17, "", False),
            "DATA": (18, datafile, True)}
    for name, w in want.items():
        if parts.get(name) != w:
            errors.append("%s is %s, expected (number, file, fills to end) %s"
                          % (name, parts.get(name), w))
    numbers = [v[0] for v in parts.values()]
    if len(set(numbers)) != len(numbers):
        errors.append("a GPT entry number is used twice: %s" % sorted(numbers))
    fill = [n for n, v in parts.items() if v[2]]
    if fill != ["DATA"]:
        errors.append("partitions filling to the end: %s, expected only DATA" % fill)


def nvflashxmlparse(path):
    """{name: (number, filename, fill-to-end)} from meta-tegra's parser."""
    p = subprocess.run([sys.executable, NVFLASHXMLPARSE, "-t", "rootfs", path],
                       capture_output=True, text=True, check=True)
    out = {}
    for line in p.stdout.splitlines():
        f = dict(re.findall(r'(\w+)="?([^";]*)"?', line))
        out[f["partname"]] = (int(f["partnumber"]), f["partfile"],
                              f["partfilltoend"] == "1")
    return out


def good(workdir, parts=TEMPLATE_PARTS, esp_file="EFIFILE"):
    rc, out, dst = run_layout(workdir, template(parts))
    errors = []
    if rc != 0:
        return ["exit %d: %s" % (rc, out.strip())]
    expect_boat_layout(numbered(dst), errors, esp_file=esp_file)
    # What initrd-flash does when there is no DATAFILE: every line naming
    # it goes, and the layout must stay whole - DATA just unwritten.
    with open(dst) as f:
        kept = "".join(l for l in f if "DATAFILE" not in l)
    stripped = os.path.join(workdir, "no-datafile.xml")
    with open(stripped, "w") as f:
        f.write(kept)
    try:
        expect_boat_layout(numbered(stripped), errors, datafile="", esp_file=esp_file)
    except ET.ParseError as e:
        errors.append("sed '/DATAFILE/d' leaves XML that does not parse: %s" % e)
    if os.path.exists(NVFLASHXMLPARSE):
        expect_boat_layout(nvflashxmlparse(dst), errors, esp_file=esp_file)
        print("      (also read back through %s)" % os.path.relpath(NVFLASHXMLPARSE))
    else:
        print("      (meta-tegra not checked out: nvflashxmlparse not run)")
    return errors


def refused(xml, says, numbers=NUMBERS):
    def check(workdir):
        rc, out, dst = run_layout(workdir, xml, numbers)
        errors = []
        if rc == 0:
            errors.append("accepted it")
        if not re.search(says, out):
            errors.append("output never matched %r: %s" % (says, out.strip()))
        if os.path.exists(dst):
            errors.append("wrote %s anyway" % os.path.basename(dst))
        return errors
    return check


def host_script(script, build_conf, unpack_ab=None, env=None, says=(), not_says=()):
    """Run scripts/<script> against a scratch build directory (local.conf
    holding build_conf) and, for 05, a scratch unpack (unpack_ab: whether it
    has APP_b) whose flasher only says it ran."""
    def check(workdir):
        build = os.path.join(workdir, "build")
        flash = os.path.join(workdir, "flash")
        os.makedirs(os.path.join(build, "conf"))
        os.makedirs(flash)
        with open(os.path.join(build, "conf", "local.conf"), "w") as f:
            f.write(build_conf)
        with open(os.path.join(flash, ".boat-unpack-complete"), "w"):
            pass
        with open(os.path.join(flash, "external-flash.xml.in"), "w") as f:
            f.write('<partition name="APP"/>\n' + ('<partition name="APP_b"/>\n' if unpack_ab else ""))
        with open(os.path.join(flash, "initrd-flash"), "w") as f:
            f.write("#!/bin/sh\necho FLASHING\n")
        os.chmod(os.path.join(flash, "initrd-flash"), 0o755)
        stub = os.path.join(workdir, "stub")
        os.makedirs(stub)
        for name, text in (("sudo", '#!/bin/sh\nexec "$@"\n'), ("lsusb", "#!/bin/sh\necho 0955:7e19\n"),
                           ("id", "#!/bin/sh\necho 1000\n")):
            with open(os.path.join(stub, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(stub, name), 0o755)
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("BOAT_") and k not in ("AB_LAYOUT", "IMAGE")}
        e.update(PATH=stub + ":" + os.environ["PATH"], BUILD_DIR=build, FLASH_DIR=flash,
                 LAYERS_DIR=os.path.join(workdir, "no-layers"), HOME=workdir)
        e.update(env or {})
        p = subprocess.run(["bash", os.path.join(HERE, script)], capture_output=True, text=True,
                           env=e, stdin=subprocess.DEVNULL, timeout=60)
        out = p.stdout + p.stderr
        errors = []
        for pattern in says:
            if not re.search(pattern, out):
                errors.append("output never matched %r: %s" % (pattern, out.strip()))
        for pattern in not_says:
            if re.search(pattern, out):
                errors.append("output matched %r: %s" % (pattern, out.strip()))
        return errors
    return check


AB_CONF = ('BOAT_AB_LAYOUT = "1"\nBOAT_KEYS_DIR = "/keys"\nBOAT_RELEASE = "1.2.0"\n')
AB_ENV = {"BOAT_KEYS_DIR": "/keys"}


CASES = [
    ("NVIDIA's real template (ids, APP and APP_b last) becomes the boat layout",
     lambda w: good(w, NVIDIA_PARTS, esp_file="ESP_FILE")),
    ("a synthetic template numbered by position becomes the boat layout: numbers, contents, "
     "fill-to-end", good),
    ("a template that already has a DATA partition is refused",
     refused(template(extra=("DATA", None)), "already has a DATA partition")),
    ("a template with a partition that already fills to the end is refused",
     refused(template(alloc={"UDA_reserved": "0x808"}), "UDA_reserved already fills to end")),
    ("a template without APP_b is refused",
     refused(template(parts=[p for p in TEMPLATE_PARTS if p[0] != "APP_b"]), "no APP_b partition")),
    ("a template whose esp moved is refused",
     refused(template(parts=[p for p in TEMPLATE_PARTS if p[0] != "A_reserved_on_user"]),
             "esp would be GPT entry 11, expected 12")),
    ("a SWAP number another partition has is refused",
     refused(template(), "SWAP=12 is already taken by esp",
             numbers="APP=1,APP_b=2,esp=12,SWAP=12,DATA=18")),
    ("a template with an id used twice is refused",
     refused(template(extra=("extra", "3")), "partition number 3 used twice")),
    # The host scripts' guards.
    ("03-build.sh refuses a min-bundle-version local.conf does not have (set after configure)",
     host_script("03-build.sh", AB_CONF, env=dict(AB_ENV, BOAT_MIN_BUNDLE_VERSION="1.2.0"),
                 says=[r"BOAT_MIN_BUNDLE_VERSION=1\.2\.0 here, but .* has ''.*02-configure-build\.sh again"])),
    ("03-build.sh refuses a release local.conf does not have",
     host_script("03-build.sh", AB_CONF, env=dict(AB_ENV, BOAT_RELEASE="1.3.0"),
                 says=[r"BOAT_RELEASE=1\.3\.0 here, but .* has '1\.2\.0'"])),
    ("03-build.sh goes on with what local.conf has exported, and says what it builds",
     host_script("03-build.sh", AB_CONF, env=AB_ENV,
                 says=[r"A/B release '1\.2\.0', min-bundle-version ''"], not_says=["02-configure-build.sh again"])),
    ("03-build.sh goes on with nothing exported, for a build directory configured with its own keys directory",
     host_script("03-build.sh", AB_CONF,
                 says=[r"A/B release '1\.2\.0'"], not_says=["02-configure-build.sh again"])),
    ("03-build.sh refuses a keys directory exported here that local.conf does not have",
     host_script("03-build.sh", AB_CONF, env={"BOAT_KEYS_DIR": "/other-keys"},
                 says=[r"BOAT_KEYS_DIR=/other-keys here, but .* has '/keys'"])),
    ("05-flash-nvme.sh refuses a single-slot unpack for an A/B build, with AB_LAYOUT unset here",
     host_script("05-flash-nvme.sh", AB_CONF, unpack_ab=False,
                 says=["the build is A/B .*holds a single-slot image"], not_says=["FLASHING"])),
    ("05-flash-nvme.sh refuses AB_LAYOUT=0 here for an A/B build",
     host_script("05-flash-nvme.sh", AB_CONF, unpack_ab=True, env={"AB_LAYOUT": "0"},
                 says=["AB_LAYOUT=0 here, but .* is configured for A/B"], not_says=["FLASHING"])),
    ("05-flash-nvme.sh flashes the A/B unpack of an A/B build, with AB_LAYOUT unset here",
     host_script("05-flash-nvme.sh", AB_CONF, unpack_ab=True, says=["FLASHING"])),
    ("05-flash-nvme.sh refuses an A/B unpack for a single-slot build",
     host_script("05-flash-nvme.sh", 'MACHINE = "x"\n', unpack_ab=True,
                 says=["holds an A/B image, but the build is single-slot"], not_says=["FLASHING"])),
]


def main():
    failed = 0
    with tempfile.TemporaryDirectory(prefix="ab-layout-") as work:
        for i, (name, check) in enumerate(CASES):
            workdir = os.path.join(work, str(i))
            os.makedirs(workdir)
            errors = check(workdir)
            if errors:
                failed += 1
                print("FAIL: %s" % name)
                for e in errors:
                    print("    " + e)
            else:
                print("ok:   %s" % name)
    print("%d of %d cases passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
