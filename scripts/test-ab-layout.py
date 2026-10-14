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
this repository. This runs it on a synthetic template with the same shape
(the partitions in NVIDIA's order, no explicit ids, the GPT entries around
them) and checks the result: numbers, names, contents and fill-to-end, also
after the flasher's own `sed '/DATAFILE/d'`, and that each template it must
not accept is refused. When meta-tegra is checked out (yocto/layers, or
META_TEGRA=<dir>), the result is also read back through meta-tegra's own
nvflashxmlparse, the parser the flash itself uses.

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

# NVIDIA's external rootfs A/B layout, in its order: the slots first, then
# kernels, recovery and the ESPs, numbered by position (no id attributes).
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
    for name, filename in parts:
        xml += part(name, "data", filename)
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


def expect_boat_layout(parts, errors, datafile="DATAFILE"):
    want = {"APP": (1, "APPFILE", False), "APP_b": (2, "", False),
            "esp": (12, "EFIFILE", False), "SWAP": (17, "", False),
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


def good(workdir):
    rc, out, dst = run_layout(workdir, template())
    errors = []
    if rc != 0:
        return ["exit %d: %s" % (rc, out.strip())]
    expect_boat_layout(numbered(dst), errors)
    # What initrd-flash does when there is no DATAFILE: every line naming
    # it goes, and the layout must stay whole - DATA just unwritten.
    with open(dst) as f:
        kept = "".join(l for l in f if "DATAFILE" not in l)
    stripped = os.path.join(workdir, "no-datafile.xml")
    with open(stripped, "w") as f:
        f.write(kept)
    try:
        expect_boat_layout(numbered(stripped), errors, datafile="")
    except ET.ParseError as e:
        errors.append("sed '/DATAFILE/d' leaves XML that does not parse: %s" % e)
    if os.path.exists(NVFLASHXMLPARSE):
        expect_boat_layout(nvflashxmlparse(dst), errors)
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


CASES = [
    ("NVIDIA's shape becomes the boat layout: numbers, contents, fill-to-end", good),
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
