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
the build's own placeholders filled in. Some scenarios start one step
earlier, from the config built into grubaa64.efi
(layers/meta-boat/recipes-bsp/grub/files/cfg), which runs grub.cfg and
resets when grub.cfg is missing or truncated.

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
# The config built into grubaa64.efi, which runs grub.cfg.
EMBEDDED_CFG = os.path.join(HERE, "..", "layers", "meta-boat", "recipes-bsp",
                            "grub", "files", "cfg")

DISK = "/dev/nvme0n1"
PART = {"A": 1, "B": 2}
KERNEL_ARGS = "console=ttyTCU0,115200 sim.kernel_args=1"
CBOOTARGS = "sim.cbootargs=1"

# The grubenv boat-grub-esp ships: slot A flashed and committed, B empty.
FLASHED = dict(ORDER="A B", A_OK="1", A_TRY="0", A_COMMITTED="1",
               B_OK="0", B_TRY="0", B_COMMITTED="0", BOAT_STAY="0")

ESP_PART = 12
ESP_DIR = "/EFI/BOOT"
# grub-emu has no built-in config, so the embedded one is run as the
# grub.cfg of another directory, which grub-emu is pointed at instead.
EMB_DIR = "/EFI/EMB"

# grub-emu has every module linked in; the board's grubaa64.efi has only
# GRUB_BUILDIN and their dependencies, and GRUB merely prints an error for a
# command it cannot find - the script carries on. So check separately that
# every command grub.cfg runs comes from a module the board's GRUB has.
# OE-core kirkstone's default (grub-efi_2.06.bb), which the bbappend extends:
OE_GRUB_BUILDIN = ("boot linux ext2 fat serial part_msdos part_gpt normal "
                   "efi_gop iso9660 configfile search loadenv test").split()
GRUB_BBAPPEND = os.path.join(HERE, "..", "layers", "meta-boat", "recipes-bsp",
                             "grub", "grub-efi_%.bbappend")
# Registered by GRUB's kernel itself (grub-core/kern/corecmd.c), so in no
# module and not in command.lst.
CORE_COMMANDS = {"set", "unset", "insmod", "ls"}
SCRIPT_WORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{"}

STUBS = r"""
# --- test stubs (scripts/test-grub-ab.py) ---
function stub_linux {
    echo "BOAT-LINUX: $*"
    # A GRUB function returns the status of its last statement.
    if [ "$SIM_LINUX_FAIL" = 1 -o "$SIM_LINUX_FAIL_SLOT" = "$chosen" ]; then false; fi
}
function stub_initrd {
    echo "BOAT-INITRD: $*"
    if [ "$SIM_INITRD_FAIL" = 1 ]; then false; fi
}
function stub_boot {
    if [ "$SIM_BOOT_RETURNS" = 1 ]; then
        echo "BOAT-BOOT-RETURNED"
        false
    else
        echo "BOAT-RESULT: boot"
        reboot
    fi
}
function stub_reboot {
    echo "BOAT-RESULT: reboot"
    reboot
}
# --- end of test stubs ---
"""


def stub(text, subs):
    for pattern, repl in subs:
        text, n = re.subn(pattern, repl, text, flags=re.M)
        if n == 0:
            sys.exit("a GRUB config no longer has a line matching %r - "
                     "update this test's stubs" % pattern)
    return text


def build_embedded():
    with open(EMBEDDED_CFG) as f:
        text = f.read()
    return STUBS + stub(text, [(r"^reboot$", "stub_reboot"),
                               (r"^sleep \d+$", "true")])


def rescue_parser_errors():
    """GRUB runs its built-in config with the rescue parser, which takes one
    plain command per line: no comments, blocks, functions or ';'."""
    errors = []
    with open(EMBEDDED_CFG) as f:
        for n, line in enumerate(f, 1):
            words = line.split()
            if not words:
                errors.append("%s:%d: an empty line" % (EMBEDDED_CFG, n))
            elif line.lstrip().startswith("#") or ";" in line or \
                    words[0] in SCRIPT_WORDS | {"fi", "done", "for",
                                                "function", "}"}:
                errors.append("%s:%d: the rescue parser runs only plain "
                              "commands: %s" % (EMBEDDED_CFG, n, line.strip()))
    return errors


def build_cfg(sim):
    with open(CFG_IN) as f:
        text = f.read()
    for key, value in (("@KERNEL_ARGS@", KERNEL_ARGS), ("@DISK@", DISK),
                       ("@PARTNUM_A@", str(PART["A"])),
                       ("@PARTNUM_B@", str(PART["B"]))):
        text = text.replace(key, value)
    leftover = re.findall(r"@[A-Z_]+@", text)
    if leftover:
        sys.exit("unfilled placeholders in grub.cfg.in: %s" % leftover)
    text = stub(text, [
        (r"^(\s*)linux \(", r"\1stub_linux ("),
        (r"^(\s*)initrd \(", r"\1stub_initrd ("),
        (r"^(\s*)boot$", r"\1stub_boot"),
        (r"^(\s*)reboot$", r"\1stub_reboot"),
        (r"^(\s*)sleep \d+$", r"\1true"),
    ])
    head = STUBS
    for key, value in sorted(sim.items()):
        head += 'set %s="%s"\n' % (key, value)
    return head + text


def make_disk(workdir, files, emb=None):
    """A GPT disk image with APP (1), APP_b (2) and a FAT esp (12) holding
    files ({name: bytes}) in ESP_DIR, and emb (bytes) as EMB_DIR/grub.cfg.
    Returns (image, esp byte offset)."""
    disk = os.path.join(workdir, "disk.img")
    esp = os.path.join(workdir, "esp.img")
    with open(disk, "wb") as f:
        f.truncate(80 << 20)
    subprocess.run(["sgdisk", "-o",
                    "-n", "1:2048:+1M", "-c", "1:APP",
                    "-n", "2:0:+1M", "-c", "2:APP_b",
                    "-n", "%d:0:+64M" % ESP_PART, "-t", "%d:EF00" % ESP_PART,
                    "-c", "%d:esp" % ESP_PART, disk],
                   check=True, capture_output=True)
    info = subprocess.run(["sgdisk", "-i", str(ESP_PART), disk], check=True,
                          capture_output=True, text=True).stdout
    start = int(re.search(r"First sector: (\d+)", info).group(1))
    # As meta-tegra's tegra-espimage makes it: 64 MiB, FAT32, which gives
    # 512-byte clusters - the 1 KiB grubenv spans two of them.
    subprocess.run(["mkfs.vfat", "-F", "32", "-C", esp, "65536"], check=True,
                   capture_output=True)
    subprocess.run(["mmd", "-i", esp, "::/EFI", "::" + ESP_DIR, "::" + EMB_DIR],
                   check=True)
    copies = [(ESP_DIR, name, data) for name, data in files.items()]
    if emb is not None:
        copies.append((EMB_DIR, "grub.cfg", emb))
    for i, (where, name, data) in enumerate(copies):
        local = os.path.join(workdir, "copy%d" % i)
        with open(local, "wb") as f:
            f.write(data)
        subprocess.run(["mcopy", "-i", esp, local, "::%s/%s" % (where, name)],
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


def run_grub(workdir, disk, read_only=False, directory=ESP_DIR):
    devmap = os.path.join(workdir, "device.map")
    with open(devmap, "w") as f:
        f.write("(hd0) %s\n" % disk)
    # grub-emu wants a terminal; script(1) gives it one. A wide terminal
    # keeps the long kernel command line on one output line.
    cmd = ("stty cols 1000 2>/dev/null; exec %sgrub-emu -m '%s' -r hd0,gpt%d -d %s"
           % (unprivileged() if read_only else "", devmap, ESP_PART, directory))
    if read_only:
        # A disk GRUB can read but not write: every save_env fails, the way
        # it would on an ESP with a failing write path.
        os.chmod(disk, 0o444)
    proc = subprocess.run(["script", "-qec", cmd, "/dev/null"],
                          capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", proc.stdout).replace("\r", "")


def scenario(name, env, expect, env_after=None, grubenv="valid", bak=None,
             sim=None, grub_errors=(), read_only=False, says=(), loads=1,
             embedded=False, grubcfg="whole"):
    """env: grubenv contents. expect: dict with result, slot, flags - the
    slot and flags of the LAST kernel load. env_after: the keys grubenv must
    have changed to (None = the file is not written at all). grubenv:
    "valid", "torn", "empty" or "missing". bak: grubenv.bak contents, or
    None for no backup. sim: stub switches - SIM_LINUX_FAIL=1 (every kernel
    load fails), SIM_LINUX_FAIL_SLOT=X (only slot X's), SIM_INITRD_FAIL=1,
    SIM_BOOT_RETURNS=1 (boot comes back instead of starting the kernel).
    grub_errors: regexes of the GRUB error lines this scenario is expected
    to print. read_only: the disk refuses every write. says: regexes the
    output must match - proof that the path under test was the one taken.
    loads: how many kernel loads it takes. embedded: start from the config
    built into grubaa64.efi, which runs grub.cfg, instead of from grub.cfg.
    grubcfg: "whole", "truncated" (cut off inside a function) or
    "missing"."""
    return dict(name=name, env=env, expect=expect, env_after=env_after,
                grubenv=grubenv, bak=bak, sim=sim or {},
                grub_errors=grub_errors, read_only=read_only, says=says,
                loads=loads, embedded=embedded, grubcfg=grubcfg)


# grub-emu leaves $cmdpath empty, so the built-in config's search.file finds
# nothing and $root stays the one grub-emu was given: the ESP, as on the
# board.
NO_CMDPATH = r"error: no such device: \(\)/EFI/BOOT/grub\.cfg\."


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
             sim=dict(SIM_LINUX_FAIL=1)),
    scenario("committed A's kernel fails to load: A marked bad, reset",
             flashed(),
             dict(result="reboot", slot="A", flags=[]),
             env_after=dict(A_OK="0", A_TRY="1"),
             sim=dict(SIM_LINUX_FAIL=1)),
    scenario("initrd fails to load: slot marked bad, reset",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             dict(result="reboot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_OK="0", B_TRY="1"),
             sim=dict(SIM_INITRD_FAIL=1)),
    scenario("boot returns: slot marked bad, reset",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             dict(result="reboot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_OK="0", B_TRY="1"),
             sim=dict(SIM_BOOT_RETURNS=1),
             says=[r"BOAT-BOOT-RETURNED"]),
    scenario("last resort prefers the committed slot: B, not a failed A",
             flashed(ORDER="B A", A_OK=0, A_COMMITTED=0, B_OK=1, B_TRY=2,
                     B_COMMITTED=1),
             dict(result="boot", slot="B", flags=["boat.lastresort=1"]),
             env_after=dict(B_OK="0"),
             says=[r"last resort, uncounted, in this order: B A"]),
    scenario("last resort with both committed follows ORDER",
             flashed(ORDER="B A", A_TRY=2, B_OK=1, B_TRY=2, B_COMMITTED=1),
             dict(result="boot", slot="B", flags=["boat.lastresort=1"]),
             env_after=dict(A_OK="0", B_OK="0")),
    scenario("last resort: a committed slot that will not load hands over to the other",
             flashed(ORDER="B A", A_OK=0, A_COMMITTED=0, B_OK=1, B_TRY=2,
                     B_COMMITTED=1),
             dict(result="boot", slot="A", flags=["boat.lastresort=1"]),
             env_after=dict(B_OK="0"),
             sim=dict(SIM_LINUX_FAIL_SLOT="B"),
             says=[r"last resort: slot B failed to load"], loads=2),
    scenario("last resort: nothing loads, nothing more is written, reset",
             flashed(A_TRY=2),
             dict(result="reboot", slot="B", flags=["boat.lastresort=1"]),
             env_after=dict(A_OK="0"),
             sim=dict(SIM_LINUX_FAIL=1),
             says=[r"no slot could be loaded"], loads=2),
    scenario("ESP refuses writes after A is used up, B on trial: last resort A",
             flashed(A_TRY=2, B_OK=1, B_COMMITTED=0),
             dict(result="boot", slot="A", flags=["boat.lastresort=1"]),
             read_only=True,
             grub_errors=[r"error: "],
             says=[r"could not record the attempt on trial slot B"]),
    scenario("grubenv missing a key: B_COMMITTED absent counts as on trial",
             {k: v for k, v in flashed(ORDER="B A", B_OK=1).items()
              if k != "B_COMMITTED"},
             dict(result="boot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_TRY="1")),
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
    scenario("empty grubenv: grubenv.bak is used, nothing is written",
             None,
             dict(result="boot", slot="B", flags=[]),
             grubenv="empty",
             bak=flashed(ORDER="B A", B_OK=1, B_COMMITTED=1),
             grub_errors=[r"invalid environment block|unexpected end of file"]),
    scenario("missing grubenv and no backup: built-in defaults boot A",
             None,
             dict(result="boot", slot="A", flags=[]),
             grubenv="missing",
             grub_errors=[r"file `/EFI/BOOT/grubenv(\.bak)?' not found"]),
    scenario("GRUB's $root names no partition: reset, nothing written",
             flashed(),
             dict(result="reboot"),
             sim=dict(root="hd0"),
             says=[r"cannot tell which disk GRUB was loaded from"], loads=0),
    scenario("built-in config runs grub.cfg: fresh flash boots A, counted",
             flashed(),
             dict(result="boot", slot="A", flags=[]),
             env_after=dict(A_TRY="1"),
             embedded=True, grub_errors=[NO_CMDPATH]),
    scenario("built-in config, B on trial: trial boot of B",
             flashed(ORDER="B A", B_OK=1, B_COMMITTED=0),
             dict(result="boot", slot="B", flags=["boat.trial=1"]),
             env_after=dict(B_TRY="1"),
             embedded=True, grub_errors=[NO_CMDPATH]),
    scenario("grub.cfg missing: the built-in config resets",
             flashed(),
             dict(result="reboot"),
             embedded=True, grubcfg="missing", grub_errors=[NO_CMDPATH],
             says=[r"grub\.cfg did not boot a slot - resetting"], loads=0),
    scenario("grub.cfg truncated: the built-in config resets",
             flashed(),
             dict(result="reboot"),
             env_after=dict(A_TRY="1"),
             embedded=True, grubcfg="truncated",
             grub_errors=[NO_CMDPATH, r"error: (syntax error|Incorrect command|"
                                      r"out of memory)\."],
             says=[r"grub\.cfg did not boot a slot - resetting"], loads=0),
]

LINUX_RE = re.compile(r"^BOAT-LINUX: (.*)$", re.M)
RESULT_RE = re.compile(r"^BOAT-RESULT: (\w+)$", re.M)


def check(sc, workdir):
    cfg = build_cfg(sc["sim"])
    if sc["grubcfg"] == "truncated":
        # A flash cut off part way: the file ends inside boot_chosen.
        cut = cfg.index("\n", cfg.index("stub_linux (${bootdisk}"))
        cfg = cfg[:cut + 1]
    files = {"boot.cfg": ('set cbootargs="%s"\n' % CBOOTARGS).encode()}
    if sc["grubcfg"] != "missing":
        files["grub.cfg"] = cfg.encode()
    if sc["grubenv"] == "valid":
        files["grubenv"] = make_env(workdir, sc["env"])
    elif sc["grubenv"] == "torn":
        # A write cut off mid-block: the signature is gone.
        files["grubenv"] = b"\0" * 512 + b"#" * 512
    elif sc["grubenv"] == "empty":
        files["grubenv"] = b""
    if sc["bak"] is not None:
        files["grubenv.bak"] = make_env(workdir, sc["bak"])
    emb = build_embedded().encode() if sc["embedded"] else None
    disk, offset = make_disk(workdir, files, emb)

    out = run_grub(workdir, disk, sc["read_only"],
                   EMB_DIR if sc["embedded"] else ESP_DIR)
    errors = []
    results = RESULT_RE.findall(out)
    linux = LINUX_RE.findall(out)
    exp = sc["expect"]
    if results != [exp["result"]]:
        errors.append("result %s, expected [%r]" % (results, exp["result"]))
    if len(linux) != sc["loads"]:
        errors.append("%d kernel loads, expected %d" % (len(linux), sc["loads"]))
    elif linux:
        args = linux[-1].split()
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


def cfg_commands(text):
    """The commands a GRUB script runs: the first word of every statement,
    less keywords, assignments and the script's own functions."""
    funcs = set(re.findall(r"^\s*function\s+(\w+)", text, re.M))
    cmds = set()
    for line in text.splitlines():
        line = re.sub(r"\"[^\"]*\"|'[^']*'", '""', line)
        line = re.sub(r"(^|\s)#.*$", "", line)
        for stmt in line.split(";"):
            words = stmt.split()
            while words and words[0] in SCRIPT_WORDS:
                words.pop(0)
            if not words or words[0] in ("fi", "done", "}", "function", "for") \
                    or "=" in words[0] or words[0] in funcs:
                continue
            cmds.add(words[0])
    return cmds


def check_modules():
    """Errors for grub.cfg commands the board's GRUB build would not have."""
    libdirs = [d for d in ("/usr/lib/grub/x86_64-emu", "/usr/lib/grub/i386-emu")
               if os.path.exists(os.path.join(d, "command.lst"))]
    if not libdirs:
        return ["grub-emu's command.lst/moddep.lst not found"]

    def table(name):
        with open(os.path.join(libdirs[0], name), encoding="utf-8") as f:
            return {k.lstrip("*"): v.split() for k, v in
                    (line.rstrip("\n").split(":", 1) for line in f if ":" in line)}
    cmd_mod = {k: v[0] for k, v in table("command.lst").items() if v}
    deps = table("moddep.lst")
    with open(GRUB_BBAPPEND, encoding="utf-8") as f:
        m = re.search(r'^GRUB_BUILDIN:append\s*=\s*"([^"]*)"', f.read(), re.M)
    have, todo = set(), OE_GRUB_BUILDIN + (m.group(1).split() if m else [])
    while todo:
        mod = todo.pop()
        if mod not in have:
            have.add(mod)
            todo.extend(deps.get(mod, []))
    cmds = set()
    for path in (CFG_IN, EMBEDDED_CFG):
        with open(path, encoding="utf-8") as f:
            cmds |= cfg_commands(f.read())
    errors = []
    for cmd in sorted(cmds - CORE_COMMANDS):
        if cmd not in cmd_mod:
            errors.append("grub.cfg runs '%s', which no GRUB module provides" % cmd)
        elif cmd_mod[cmd] not in have:
            errors.append("grub.cfg runs '%s' from module '%s', which is not in "
                          "GRUB_BUILDIN or its dependencies" % (cmd, cmd_mod[cmd]))
    return errors


def main():
    for tool in ("grub-emu", "grub-editenv", "sgdisk", "mkfs.vfat", "mcopy",
                 "script"):
        if not shutil.which(tool):
            print("SKIP: %s not installed (apt-get install grub-emu gdisk "
                  "dosfstools mtools)" % tool)
            return 77
    version = subprocess.run(["grub-emu", "--version"], capture_output=True,
                             text=True).stdout.strip()
    print("using %s (the board runs GRUB 2.06)" % (version or "grub-emu"))
    failed = 0
    errors = check_modules()
    for e in errors:
        print("FAIL: " + e)
    if errors:
        failed += 1
    else:
        print("ok:   every command the GRUB configs run is in the board's "
              "GRUB build")
    errors = rescue_parser_errors()
    for e in errors:
        print("FAIL: " + e)
    if errors:
        failed += 1
    else:
        print("ok:   the built-in config is plain commands only, as GRUB's "
              "rescue parser needs")
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
    total = len(SCENARIOS) + 2
    print("%d of %d checks passed" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
