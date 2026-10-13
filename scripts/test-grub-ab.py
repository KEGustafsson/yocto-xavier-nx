#!/usr/bin/env python3
"""Run meta-boat's A/B grub.cfg in grub-emu against a table of grubenv states.

    scripts/test-grub-ab.py     # needs grub-emu, grub-editenv, sgdisk,
                                # mkfs.vfat, mtools and script(1)

The slot-selection state machine in
layers/meta-boat/recipes-bsp/boat-grub-esp/files/grub.cfg.in is the part of
the A/B design that decides, with nobody at the console, which OS the boat
boots. grub-script-check only proves it parses. This runs it in GRUB itself
(grub-emu, the same script engine, test, regexp and load_env/save_env code
as the EFI build) and checks, for every scenario, which slot it boots, the
kernel command line it builds, and what it writes back to grubenv.

GRUB runs from a GPT disk image laid out like the boat's SSD where it
matters - APP at 1, APP_b at 2, a FAT esp at 12 holding grub.cfg, boot.cfg
and grubenv - because save_env only writes to a real disk (it refuses files
on grub-emu's host filesystem), and because the cfg derives the slot disk
from GRUB's own $root.

Only the five commands that would leave the emulator or the machine are
replaced, by stubs that print what they were asked to do: linux, initrd,
boot, reboot and sleep. Everything else is the file the build ships, with
the build's own placeholders filled in.

Exit status: 0 = all scenarios passed, 1 = a scenario failed, 77 = grub-emu
(or another tool) is missing - the automake "skipped" code, which
scripts/lint.sh reports as a skip.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_IN = os.path.join(HERE, "..", "layers", "meta-boat", "recipes-bsp",
                      "boat-grub-esp", "files", "grub.cfg.in")

DISK = "/dev/nvme0n1"
PART = {"A": 1, "B": 2}
KERNEL_ARGS = "console=ttyTCU0,115200 sim.kernel_args=1"
CBOOTARGS = "sim.cbootargs=1"

# The grubenv boat-grub-esp ships: slot A flashed and committed, B empty.
FLASHED = dict(ORDER="A B", A_OK="1", A_TRY="0", A_COMMITTED="1",
               B_OK="0", B_TRY="0", B_COMMITTED="0", BOAT_STAY="0")

ESP_PART = 12
ESP_DIR = "/EFI/BOOT"

STUBS = r"""
# --- test stubs (scripts/test-grub-ab.py) ---
function stub_linux {
    echo "BOAT-LINUX: $*"
    if [ "$SIM_LINUX_FAIL" = 1 ]; then false; fi
}
function stub_initrd {
    echo "BOAT-INITRD: $*"
}
function stub_boot {
    echo "BOAT-RESULT: boot"
    reboot
}
function stub_reboot {
    echo "BOAT-RESULT: reboot"
    reboot
}
# --- end of test stubs ---
"""


def build_cfg(sim_linux_fail):
    with open(CFG_IN) as f:
        text = f.read()
    for key, value in (("@KERNEL_ARGS@", KERNEL_ARGS), ("@DISK@", DISK),
                       ("@PARTNUM_A@", str(PART["A"])),
                       ("@PARTNUM_B@", str(PART["B"]))):
        text = text.replace(key, value)
    leftover = re.findall(r"@[A-Z_]+@", text)
    if leftover:
        sys.exit("unfilled placeholders in grub.cfg.in: %s" % leftover)
    subs = [
        (r"^(\s*)linux \(", r"\1stub_linux ("),
        (r"^(\s*)initrd \(", r"\1stub_initrd ("),
        (r"^(\s*)boot$", r"\1stub_boot"),
        (r"^(\s*)reboot$", r"\1stub_reboot"),
        (r"^(\s*)sleep \d+$", r"\1true"),
    ]
    for pattern, repl in subs:
        text, n = re.subn(pattern, repl, text, flags=re.M)
        if n == 0:
            sys.exit("grub.cfg.in no longer has a line matching %r - "
                     "update this test's stubs" % pattern)
    head = STUBS
    if sim_linux_fail:
        head += 'set SIM_LINUX_FAIL=1\n'
    return head + text


def make_disk(workdir, files):
    """A GPT disk image with APP (1), APP_b (2) and a FAT esp (12) holding
    files ({name: bytes}) in ESP_DIR. Returns (image, esp byte offset)."""
    disk = os.path.join(workdir, "disk.img")
    esp = os.path.join(workdir, "esp.img")
    with open(disk, "wb") as f:
        f.truncate(16 << 20)
    subprocess.run(["sgdisk", "-o",
                    "-n", "1:2048:+1M", "-c", "1:APP",
                    "-n", "2:0:+1M", "-c", "2:APP_b",
                    "-n", "%d:0:+8M" % ESP_PART, "-t", "%d:EF00" % ESP_PART,
                    "-c", "%d:esp" % ESP_PART, disk],
                   check=True, capture_output=True)
    info = subprocess.run(["sgdisk", "-i", str(ESP_PART), disk], check=True,
                          capture_output=True, text=True).stdout
    start = int(re.search(r"First sector: (\d+)", info).group(1))
    subprocess.run(["mkfs.vfat", "-C", esp, "8192"], check=True,
                   capture_output=True)
    subprocess.run(["mmd", "-i", esp, "::/EFI", "::" + ESP_DIR], check=True)
    for name, data in files.items():
        local = os.path.join(workdir, name)
        with open(local, "wb") as f:
            f.write(data)
        subprocess.run(["mcopy", "-i", esp, local, "::%s/%s" % (ESP_DIR, name)],
                       check=True)
    with open(esp, "rb") as src, open(disk, "r+b") as dst:
        dst.seek(start * 512)
        dst.write(src.read())
    return disk, start * 512


def unprivileged():
    """Prefix that drops root, which would write a read-only file anyway."""
    if os.geteuid() != 0:
        return ""
    return "setpriv --reuid=65534 --regid=65534 --clear-groups "


def read_esp_file(workdir, disk, offset, name):
    """The file's bytes from the image's esp, or None if it is absent."""
    out = os.path.join(workdir, "out-" + name)
    proc = subprocess.run(["mcopy", "-n", "-i", "%s@@%d" % (disk, offset),
                           "::%s/%s" % (ESP_DIR, name), out],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    with open(out, "rb") as f:
        return f.read()


def run_grub(workdir, disk, read_only=False):
    devmap = os.path.join(workdir, "device.map")
    with open(devmap, "w") as f:
        f.write("(hd0) %s\n" % disk)
    # grub-emu wants a terminal; script(1) gives it one. A wide terminal
    # keeps the long kernel command line on one output line.
    cmd = ("stty cols 1000 2>/dev/null; exec %sgrub-emu -m '%s' -r hd0,gpt%d -d %s"
           % (unprivileged() if read_only else "", devmap, ESP_PART, ESP_DIR))
    if read_only:
        # A disk GRUB can read but not write: every save_env fails, the way
        # it would on an ESP with a failing write path.
        os.chmod(disk, 0o444)
    proc = subprocess.run(["script", "-qec", cmd, "/dev/null"],
                          capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", proc.stdout).replace("\r", "")


def scenario(name, env, expect, env_after=None, grubenv="valid", bak=None,
             linux_fail=False, grub_errors=(), read_only=False, says=()):
    """env: grubenv contents. expect: dict with result, slot, flags.
    env_after: the keys grubenv must have changed to (None = the file is
    not written at all). grubenv: "valid", "torn" or "missing". bak:
    grubenv.bak contents, or None for no backup. grub_errors: regexes of
    the GRUB error lines this scenario is expected to print. read_only: the
    disk refuses every write. says: regexes the output must match - proof
    that the path under test was the one taken."""
    return dict(name=name, env=env, expect=expect, env_after=env_after,
                grubenv=grubenv, bak=bak, linux_fail=linux_fail,
                grub_errors=grub_errors, read_only=read_only, says=says)


def flashed(**kw):
    env = dict(FLASHED)
    env.update({k: str(v) for k, v in kw.items()})
    return env


SCENARIOS = [
    scenario("fresh flash boots A, counted",
             flashed(),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(A_TRY="1", A_OK="1", B_OK="0")),
    scenario("committed A after one unconfirmed boot gets its second try",
             flashed(A_TRY=1),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(A_TRY="2", A_OK="1")),
    scenario("committed A out of attempts, B empty: last resort A, uncounted",
             flashed(A_TRY=2),
             dict(result="boot", slot="A", flags=["boat.lastresort=1"]),
             env_after=dict(A_OK="0", A_TRY="2")),
    scenario("new bundle in B: trial boot of B",
             flashed(ORDER="B A", B_OK=1, B_TRY=0, B_COMMITTED=0),
             dict(result="boot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_TRY="1", B_OK="1", A_TRY="0")),
    scenario("B trial not confirmed: B marked bad, back to A",
             flashed(ORDER="B A", B_OK=1, B_TRY=1, B_COMMITTED=0),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(B_OK="0", A_TRY="1", A_OK="1")),
    scenario("committed B after one power cut keeps B",
             flashed(ORDER="B A", B_OK=1, B_TRY=1, B_COMMITTED=1),
             dict(result="boot", slot="B", flags=[]),
             env_after=dict(B_TRY="2", B_OK="1")),
    scenario("committed B out of attempts falls back to committed A",
             flashed(ORDER="B A", B_OK=1, B_TRY=2, B_COMMITTED=1),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(B_OK="0", A_TRY="1")),
    scenario("A on trial (A_COMMITTED=0) gets one attempt only",
             flashed(A_TRY=1, A_COMMITTED=0, ORDER="A B", B_OK=1,
                     B_COMMITTED=1),
             dict(result="boot", slot="B", flags=[]),
             env_after=dict(A_OK="0", B_TRY="1")),
    scenario("garbage TRY counts as attempts used up",
             flashed(ORDER="B A", B_OK=1, B_TRY="x", B_COMMITTED=1),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(B_OK="0", B_TRY="2", A_TRY="1")),
    scenario("ORDER naming no known slot: last resort A",
             flashed(ORDER="C"),
             dict(result="boot", slot="A", flags=["boat.lastresort=1"]),
             env_after=None),
    scenario("BOAT_STAY=1 is passed to the kernel",
             flashed(BOAT_STAY=1),
             dict(result="boot", slot="A", flags=["boat.stay=1"]),
             env_after=dict(A_TRY="1")),
    scenario("BOAT_STAY=1 on a trial passes both flags",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0, BOAT_STAY=1),
             dict(result="boot", slot="B",
                  flags=["boat.trial=1", "boat.stay=1"]),
             env_after=dict(B_TRY="1")),
    scenario("kernel fails to load: slot marked bad, reset",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             dict(result="reboot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_OK="0", B_TRY="1", A_OK="1"),
             linux_fail=True),
    scenario("torn grubenv: grubenv.bak is used, nothing is written",
             None,
             dict(result="boot", slot="B", flags=[]),
             grubenv="torn",
             bak=flashed(ORDER="B A", B_OK=1, B_COMMITTED=1),
             grub_errors=[r"invalid environment block"]),
    scenario("torn grubenv, backup has B on trial: B passed over, A boots",
             None,
             dict(result="boot", slot="A", flags=[]),
             grubenv="torn",
             bak=flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             grub_errors=[r"invalid environment block"],
             says=[r"slot B is on trial, but its attempt cannot be recorded"]),
    scenario("ESP refuses writes, B on trial: B passed over, A boots",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             dict(result="boot", slot="A", flags=[]),
             read_only=True,
             grub_errors=[r"error: "],
             says=[r"could not record the attempt on trial slot B"]),
    scenario("ESP refuses writes, committed A still boots",
             flashed(),
             dict(result="boot", slot="A", flags=[]),
             read_only=True,
             grub_errors=[r"error: "]),
    scenario("torn grubenv and no backup: built-in defaults boot A",
             None,
             dict(result="boot", slot="A", flags=[]),
             grubenv="torn",
             grub_errors=[r"invalid environment block",
                          r"file `/EFI/BOOT/grubenv\.bak' not found"]),
    scenario("missing grubenv and no backup: built-in defaults boot A",
             None,
             dict(result="boot", slot="A", flags=[]),
             grubenv="missing",
             grub_errors=[r"file `/EFI/BOOT/grubenv(\.bak)?' not found"]),
]

LINUX_RE = re.compile(r"^BOAT-LINUX: (.*)$", re.M)
RESULT_RE = re.compile(r"^BOAT-RESULT: (\w+)$", re.M)


def check(sc, workdir):
    files = {
        "grub.cfg": build_cfg(sc["linux_fail"]).encode(),
        "boot.cfg": ('set cbootargs="%s"\n' % CBOOTARGS).encode(),
    }
    if sc["grubenv"] == "valid":
        files["grubenv"] = make_env(workdir, sc["env"])
    elif sc["grubenv"] == "torn":
        # A write cut off mid-block: the signature is gone.
        files["grubenv"] = b"\0" * 512 + b"#" * 512
    if sc["bak"] is not None:
        files["grubenv.bak"] = make_env(workdir, sc["bak"])
    disk, offset = make_disk(workdir, files)

    out = run_grub(workdir, disk, sc["read_only"])
    errors = []
    results = RESULT_RE.findall(out)
    linux = LINUX_RE.findall(out)
    exp = sc["expect"]
    if results != [exp["result"]]:
        errors.append("result %s, expected [%r]" % (results, exp["result"]))
    if len(linux) != 1:
        errors.append("%d kernel loads, expected 1" % len(linux))
    else:
        args = linux[0].split()
        slot = exp["slot"]
        want = ["(hd0,gpt%d)/boot/Image" % PART[slot], CBOOTARGS] + \
            KERNEL_ARGS.split() + \
            ["root=%sp%d" % (DISK, PART[slot]), "rootwait", "rw",
             "rootfstype=ext4", "rauc.slot=%s" % slot, "panic=10"] + \
            exp["flags"]
        if args != want:
            errors.append("kernel command line\n      got:  %s\n      want: %s"
                          % (" ".join(args), " ".join(want)))
    for pattern in sc["says"]:
        if not re.search(pattern, out):
            errors.append("output never matched %r" % pattern)
    unexpected = [line for line in out.splitlines() if "error:" in line and
                  not any(re.search(ok, line) for ok in sc["grub_errors"])]
    if unexpected:
        errors.append("GRUB reported an error:\n" + "\n".join(
            "      " + line for line in unexpected))

    after = read_esp_file(workdir, disk, offset, "grubenv")
    if sc["env_after"] is None:
        if after != files.get("grubenv"):
            errors.append("grubenv was written, expected it untouched")
    else:
        want_env = dict(sc["env"])
        want_env.update(sc["env_after"])
        got = parse_env(workdir, after)
        if got != want_env:
            errors.append("grubenv afterwards\n      got:  %s\n      want: %s"
                          % (fmt_env(got), fmt_env(want_env)))
        if after is None or len(after) != 1024:
            errors.append("grubenv is no longer a 1 KiB block")
    return errors, out


def make_env(workdir, env):
    path = os.path.join(workdir, "env.tmp")
    if os.path.exists(path):
        os.unlink(path)
    subprocess.run(["grub-editenv", path, "create"], check=True)
    subprocess.run(["grub-editenv", path, "set"] +
                   ["%s=%s" % kv for kv in env.items()], check=True)
    with open(path, "rb") as f:
        return f.read()


def parse_env(workdir, data):
    if data is None:
        return {}
    path = os.path.join(workdir, "env.read")
    with open(path, "wb") as f:
        f.write(data)
    out = subprocess.run(["grub-editenv", path, "list"], check=True,
                         capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def fmt_env(env):
    return " ".join("%s=%s" % kv for kv in sorted(env.items()))


def main():
    for tool in ("grub-emu", "grub-editenv", "sgdisk", "mkfs.vfat", "mcopy",
                 "script"):
        if not shutil.which(tool):
            print("SKIP: %s not installed (apt-get install grub-emu gdisk "
                  "dosfstools mtools)" % tool)
            return 77
    failed = 0
    with tempfile.TemporaryDirectory(prefix="grub-ab-") as workdir:
        # Readable by the unprivileged user the read-only scenarios run as.
        os.chmod(workdir, 0o755)
        for i, sc in enumerate(SCENARIOS):
            scdir = os.path.join(workdir, str(i))
            os.makedirs(scdir)
            errors, out = check(sc, scdir)
            if errors:
                failed += 1
                print("FAIL: %s" % sc["name"])
                for e in errors:
                    print("    " + e)
                if os.environ.get("VERBOSE"):
                    print(out)
            else:
                print("ok:   %s" % sc["name"])
    print("%d of %d scenarios passed" % (len(SCENARIOS) - failed, len(SCENARIOS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
