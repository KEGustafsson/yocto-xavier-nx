#!/usr/bin/env python3
"""Run boat-storage, as shipped, against a fake board.

    scripts/test-boat-storage.py

boat-storage decides, at every boot of an A/B image, whether a partition
is made swap, whether DATA is mounted, and what is copied onto it - on
partitions that may hold the boat's only copy of its data. This runs the
script against a scratch directory standing in for the board: /proc/mounts
and /proc/swaps, the block devices that exist, and what blkid reports for
each partition (GPT name, filesystem, label). mkswap, swapon, e2fsck,
mkfs.ext4, resize2fs (which can report that it grew, or fail), df and sync
are stubs that log how they were called,
so each case checks exactly what would have been run - and what never was.

Exit status: 0 = all passed, 1 = a case failed.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER = os.path.join(HERE, "..", "layers", "meta-boat")
LIB = os.path.join(LAYER, "recipes-boat", "ab-boot", "files", "boat-ab-lib.sh")
SCRIPT = os.path.join(LAYER, "recipes-boat", "storage", "files", "boat-storage.sh")

DISK = "/dev/nvme0n1"
SWAP = DISK + "p17"
DATA = DISK + "p18"
NAMES = {"1": "APP", "2": "APP_b", "12": "esp", "17": "SWAP", "18": "DATA"}
TYPES = {"1": "ext4", "2": "ext4", "12": "vfat", "18": "ext4"}
LABELS = {"18": "boat-data"}


def slurp(path):
    with open(path) as f:
        return f.read()


class Board:
    def __init__(self, root, names=None, types=None, labels=None, swaps=(),
                 data_mounted=True, blkid_fails=(), e2fsck_rc=0, home=True,
                 seeded=False, cmdline="root=x rauc.slot=A", root_part="1",
                 resize="nothing"):
        """cmdline: the kernel command line. root_part: the partition /
        is mounted from. resize: what resize2fs does - "nothing" (already
        full size), "grew" or "fails"."""
        self.root = root
        for d in ("bin", "proc", "data", "home"):
            os.makedirs(self.path(d), exist_ok=True)
        self.data = self.path("data")
        table = lambda t: "".join("%s %s\n" % kv for kv in t.items())
        self.write("names", table(NAMES if names is None else names))
        self.write("types", table(TYPES if types is None else types))
        self.write("labels", table(LABELS if labels is None else labels))
        self.write("devices", "".join("%sp%s\n" % (DISK, n) for n in NAMES))
        self.write("blkid-fails", "".join(n + "\n" for n in blkid_fails))
        self.write("e2fsck-rc", "%d\n" % e2fsck_rc)
        mounts = ["%sp%s / ext4 rw 0 0" % (DISK, root_part)]
        if data_mounted:
            mounts.append("%s %s ext4 rw 0 0" % (DATA, self.data))
        self.write("proc/mounts", "".join(m + "\n" for m in mounts))
        self.write("proc/swaps", "Filename Type Size Used Priority\n" +
                   "".join("%s partition 8388604 0 -2\n" % s for s in swaps))
        self.write("proc/cmdline", cmdline + "\n")
        self.write("resize", resize + "\n")
        if home:
            os.makedirs(self.path("home/boat/.ssh"))
            self.write("home/boat/.ssh/authorized_keys", "ssh-ed25519 AAAA boat\n")
        if seeded:
            os.makedirs(self.path("data/home/boat"))
            self.write("data/home/boat/notes", "already on DATA\n")
        self.stubs()
        self.install()

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        with open(self.path(rel), "w") as f:
            f.write(text)

    def stubs(self):
        r = self.root
        # blkid -p -o value -s <TAG> <dev>: the partition number picks the
        # row, the tag picks the table.
        self.write("bin/blkid", """#!/bin/sh
tag=""
while [ "$#" -gt 1 ]; do
    [ "$1" = -s ] && tag="$2"
    shift
done
num="${1##*p}"
grep -qx "$num" %(r)s/blkid-fails && exit 8
case "$tag" in
    PART_ENTRY_NAME) table=names ;;
    TYPE) table=types ;;
    LABEL) table=labels ;;
    *) exit 4 ;;
esac
awk -v n="$num" '$1 == n { print $2; f = 1 } END { exit !f }' %(r)s/$table || exit 2
""" % dict(r=r))
        self.write("bin/e2fsck", """#!/bin/sh
echo "e2fsck $*" >> %(r)s/calls
exit "$(cat %(r)s/e2fsck-rc)"
""" % dict(r=r))
        self.write("bin/resize2fs", """#!/bin/sh
echo "resize2fs $*" >> %(r)s/calls
case "$(cat %(r)s/resize)" in
    grew) echo "The filesystem on $1 is now 4000000 (4k) blocks long." ;;
    fails) echo "resize2fs: Permission denied to resize filesystem"; exit 1 ;;
    *) echo "The filesystem is already 1000 blocks long.  Nothing to do!" ;;
esac
""" % dict(r=r))
        self.write("bin/df", "#!/bin/sh\necho \"Filesystem Size\"\n")
        names = ["blkid", "e2fsck", "resize2fs", "df"]
        for cmd in ("mkswap", "swapon", "mkfs.ext4", "sync"):
            self.write("bin/" + cmd, "#!/bin/sh\necho \"%s $*\" >> %s/calls\n" % (cmd, r))
            names.append(cmd)
        for name in names:
            os.chmod(self.path("bin/" + name), 0o755)

    def install(self):
        r = self.root
        is_dev = '! grep -qx "$1" %s/devices' % r
        lib = slurp(LIB)
        for old, new in (("@DISK@", DISK), ("@PARTNUM_A@", "1"),
                         ("@PARTNUM_B@", "2"), ("@PARTNUM_ESP@", "12"),
                         ("@PARTNUM_SWAP@", "17"), ("@PARTNUM_DATA@", "18"),
                         ("/proc/cmdline", r + "/proc/cmdline"),
                         ('[ ! -b "$1" ]', is_dev)):
            lib = lib.replace(old, new)
        assert "@" not in lib.replace("$@", ""), "lib.sh has an unfilled placeholder"
        self.write("lib.sh", lib)
        text = slurp(SCRIPT)
        for old, new in (("/usr/lib/boat-ab/lib.sh", r + "/lib.sh"),
                         ("@HOME_USERS@", "boat"),
                         ("DATA_MOUNT=/data", "DATA_MOUNT=" + self.data),
                         ('src="/home/$user"', 'src="%s/home/$user"' % r),
                         ("/proc/mounts", r + "/proc/mounts"),
                         ("/proc/swaps", r + "/proc/swaps"),
                         ('[ ! -b "$dev" ]', is_dev.replace("$1", "$dev")),
                         ('[ -b "$dev" ]', "grep -qx \"$dev\" %s/devices" % r)):
            text = text.replace(old, new)
        assert "@" not in text.replace("$@", ""), "boat-storage has an unfilled placeholder"
        self.write("boat-storage", text)

    def run(self, *args):
        env = dict(os.environ, PATH=self.path("bin") + ":" + os.environ["PATH"])
        p = subprocess.run(["sh", self.path("boat-storage")] + list(args),
                           capture_output=True, text=True, env=env,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def calls(self):
        path = self.path("calls")
        return slurp(path).splitlines() if os.path.exists(path) else []


def case(name, args, expect_rc, board=None, calls=None, says=(), check=None):
    return dict(name=name, args=args, expect_rc=expect_rc, board=board or {},
                calls=calls, says=says, check=check)


def seeded_home(b):
    want = "ssh-ed25519 AAAA boat\n"
    got = os.path.join(b.data, "home/boat/.ssh/authorized_keys")
    if not os.path.exists(got) or slurp(got) != want:
        return ["/data/home/boat is not a copy of the image's /home/boat"]
    if os.path.exists(os.path.join(b.data, "home/boat.seeding")):
        return ["the .seeding copy was left behind"]
    return []


def home_untouched(b):
    if sorted(os.listdir(os.path.join(b.data, "home/boat"))) != ["notes"]:
        return ["an already seeded home was seeded again"]
    return []


CASES = [
    # --- swap ------------------------------------------------------------
    case("swap: a blank SWAP is made swap, then enabled", ["swap"], 0,
         calls=["mkswap -L boat-swap " + SWAP, "swapon " + SWAP],
         says=["holds no signature - making it swap"]),
    case("swap: existing swap is only enabled", ["swap"], 0,
         board=dict(types=dict(TYPES, **{"17": "swap"})),
         calls=["swapon " + SWAP]),
    case("swap: swap already in use - nothing to do", ["swap"], 0,
         board=dict(types=dict(TYPES, **{"17": "swap"}), swaps=[SWAP]),
         calls=[], says=["already in use"]),
    case("swap: a hibernation image is left alone", ["swap"], 1,
         board=dict(types=dict(TYPES, **{"17": "swsuspend"})),
         calls=[], says=["hibernation image"]),
    case("swap: another layout's partition 17 is not touched", ["swap"], 1,
         board=dict(names=dict(NAMES, **{"17": "UDA"})),
         calls=[], says=["expected 'SWAP'"]),
    # --- check-data ------------------------------------------------------
    case("check-data: the flashed boat-data ext4 is checked, then mounted", ["check-data"], 0,
         calls=["e2fsck -p " + DATA]),
    case("check-data: e2fsck fixed something - still mounted", ["check-data"], 0,
         board=dict(e2fsck_rc=1), calls=["e2fsck -p " + DATA]),
    case("check-data: e2fsck needs a person - not mounted", ["check-data"], 1,
         board=dict(e2fsck_rc=4), calls=["e2fsck -p " + DATA],
         says=["will not fix unattended"]),
    case("check-data: another label - not checked, not mounted", ["check-data"], 1,
         board=dict(labels={"18": "scratch"}), calls=[], says=["not the ext4 'boat-data'"]),
    case("check-data: no filesystem - not checked, not mounted", ["check-data"], 1,
         board=dict(types=dict(TYPES, **{"18": ""}), labels={}), calls=[],
         says=["holds no filesystem"]),
    case("check-data: another layout's partition 18 is not touched", ["check-data"], 1,
         board=dict(names=dict(NAMES, **{"18": "UDA"})), calls=[], says=["expected 'DATA'"]),
    # --- prepare-data ----------------------------------------------------
    case("prepare-data: the home is copied onto DATA, on disk before and after the rename",
         ["prepare-data"], 0, check=seeded_home, says=["seeded"],
         calls=["sync -f {data}", "sync -f {data}"]),
    case("prepare-data: a home already on DATA is never seeded again", ["prepare-data"], 0,
         board=dict(seeded=True), calls=[], check=home_untouched),
    case("prepare-data: refuses when /data is not DATA", ["prepare-data"], 1,
         board=dict(data_mounted=False), calls=[], says=["not mounted from DATA"]),
    # --- format-data -----------------------------------------------------
    case("format-data: refuses while DATA is mounted", ["format-data", "--yes"], 1,
         calls=[], says=["is mounted"]),
    case("format-data: refuses another layout's partition 18", ["format-data", "--yes"], 1,
         board=dict(names=dict(NAMES, **{"18": "UDA"}), data_mounted=False), calls=[],
         says=["refusing to format"]),
    case("format-data: asks first, and does nothing without the answer", ["format-data"], 1,
         board=dict(data_mounted=False), calls=[], says=["not confirmed"]),
    case("format-data: --yes makes a new boat-data ext4", ["format-data", "--yes"], 0,
         board=dict(data_mounted=False),
         calls=["mkfs.ext4 -F -q -b 4096 -m 1 -L boat-data " + DATA]),
    # --- grow ------------------------------------------------------------
    case("grow: DATA and slot A's root filesystem are grown", ["grow"], 0,
         board=dict(resize="grew"),
         calls=["resize2fs " + DATA, "resize2fs %sp1" % DISK],
         says=["grew the filesystem on %s" % DATA, r"grew the filesystem on %sp1 \(/\)" % DISK]),
    case("grow: on slot B it grows APP_b, never APP", ["grow"], 0,
         board=dict(cmdline="rauc.slot=B", root_part="2"),
         calls=["resize2fs " + DATA, "resize2fs %sp2" % DISK]),
    case("grow: a root not mounted from the booted slot is not grown", ["grow"], 0,
         board=dict(cmdline="rauc.slot=A", root_part="2"),
         calls=["resize2fs " + DATA], says=["/ is not mounted from %sp1 - not growing it" % DISK]),
    case("grow: DATA not mounted - only the root is grown", ["grow"], 0,
         board=dict(data_mounted=False), calls=["resize2fs %sp1" % DISK],
         says=["is not mounted from %s - not growing it" % DATA]),
    case("grow: another layout's partition is not grown", ["grow"], 0,
         board=dict(names=dict(NAMES, **{"1": "UDA", "18": "UDA2"})), calls=[],
         says=["expected 'APP'", "expected 'DATA'"]),
    case("grow: booted outside GRUB - the root is not grown", ["grow"], 0,
         board=dict(cmdline="root=/dev/nvme0n1p1"), calls=["resize2fs " + DATA],
         says=["booted outside GRUB"]),
    case("grow: a resize2fs that fails is reported, and the unit fails", ["grow"], 1,
         board=dict(resize="fails"),
         calls=["resize2fs " + DATA, "resize2fs %sp1" % DISK],
         says=["resize2fs %s failed" % DATA, "Permission denied"]),
    # --- status ----------------------------------------------------------
    case("status: a partition blkid cannot probe still gets its line", ["status"], 0,
         board=dict(blkid_fails=["17"]),
         says=[r"APP\s+%sp1" % DISK, r"SWAP\s+%s\s+GPT \"\?\"" % SWAP,
               r"DATA\s+%s" % DATA, r"expected GPT name \"SWAP\""]),
]


def check(c, root):
    b = Board(root, **c["board"])
    rc, out = b.run(*c["args"])
    errors = []
    if rc != c["expect_rc"]:
        errors.append("exit %d, expected %d" % (rc, c["expect_rc"]))
    for pattern in c["says"]:
        if not re.search(pattern, out):
            errors.append("output never matched %r" % pattern)
    if c["calls"] is not None:
        want = [w.format(data=b.data) for w in c["calls"]]
        if b.calls() != want:
            errors.append("ran %r, expected %r" % (b.calls(), want))
    if c["check"]:
        errors += c["check"](b)
    return errors, out


def main():
    failed = 0
    with tempfile.TemporaryDirectory(prefix="boat-storage-") as work:
        for i, c in enumerate(CASES):
            root = os.path.join(work, str(i))
            os.makedirs(root)
            errors, out = check(c, root)
            if errors:
                failed += 1
                print("FAIL: %s" % c["name"])
                for e in errors:
                    print("    " + e)
                print("    output:\n" + "\n".join("      " + l for l in out.splitlines()))
            else:
                print("ok:   %s" % c["name"])
    print("%d of %d cases passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
