#!/usr/bin/env python3
"""Run publish-update.sh, as shipped, against a local channel directory -
and push-update.sh, the no-internet path, against a fake board.

    scripts/test-publish-update.py      # needs openssl

publish-update.sh decides what every boat following a channel is offered.
This makes real update keys with scripts/gen-update-keys.sh (the fleet's
and the bench's), bundles laid out the way RAUC 1.13 lays out a verity
bundle (payload, then a CMS signature that carries the manifest, then the
signature's size), and publishes them to a scratch directory. It checks
that a good bundle is published - the index signed, naming the bundle,
which is there whole, with its size and SHA-256, readable by a web server
whatever the publisher's umask - and that a bundle a board's RAUC would
refuse, a bench bundle on a fleet channel, a bundle built against another
device tree, or a copy that fails half way publishes nothing. It walks the
fleet's flows through boat-update's own index checks: promotion from
testing to stable, a release withdrawn by publishing an older one, a
version rebuilt, a second publisher whose clock is behind. It also checks
the image recipe's keyring check (rauc_%.bbappend) on the keys it makes.

push-update.sh runs against a board made of a scratch directory: ssh is a
stub that runs the command there (options as ssh takes them: -p is a port),
boat-update a stub that needs root, installs, and "reboots" (a new boot ID
and slot). It checks that a documented BOAT_SSH_OPTS port and the boat user
work, that ping is never what decides the reboot, that ssh keeps the session
alive, and that the multi-GB copy never stays on /data.

Exit status: 0 = all passed, 1 = a case failed, 77 = openssl missing.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLISH = os.path.join(HERE, "publish-update.sh")
PUSH = os.path.join(HERE, "push-update.sh")
GEN = os.path.join(HERE, "gen-update-keys.sh")
LAYER = os.path.join(HERE, "..", "layers", "meta-boat")
TOOL = os.path.join(LAYER, "external", "rauc", "recipes-boat", "boat-update", "files", "boat-update.py")
RAUC_APPEND = os.path.join(LAYER, "external", "rauc", "recipes-core", "rauc", "rauc_%.bbappend")

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok:  " if ok else "FAIL:", name, "" if ok else "\n    " + str(detail)))
    if not ok:
        failures.append(name)


def openssl(*args):
    return subprocess.run(["openssl"] + list(args), check=True, capture_output=True)


def make_bundle(path, keys, signer="rauc-signing", compatible="boat-xavier-nx", version="1.2.0",
                fmt="verity", detached=False, firmware="f" * 64):
    """A bundle as RAUC 1.13 writes a verity one: the payload, the CMS
    signature carrying the manifest, then the signature's size."""
    work = os.path.dirname(path)
    manifest = os.path.join(work, "manifest.raucm")
    with open(manifest, "w") as f:
        f.write("[update]\ncompatible=%s\nversion=%s\n\n[bundle]\nformat=%s\nverity-hash=00\n\n"
                "[meta.boat]\nl4t=35.6.4\nfirmware=%s\n" % (compatible, version, fmt, firmware))
    payload = os.path.join(work, "payload")
    with open(payload, "wb") as f:
        f.write(os.urandom(256 * 1024))
    sig = os.path.join(work, "sig.der")
    openssl("cms", "-sign", "-binary", "-outform", "DER", "-in", payload if detached else manifest,
            "-signer", os.path.join(keys, "%s.cert.pem" % signer),
            "-inkey", os.path.join(keys, "%s.key.pem" % signer), "-out", sig,
            *([] if detached else ["-nodetach"]))
    with open(payload, "rb") as p, open(sig, "rb") as s, open(path, "wb") as out:
        body = s.read()
        out.write(p.read() + body + struct.pack(">Q", len(body)))
    return path


# rsync between here and host:path, as rsync behaves without -l/-L/-a: a
# symlink is skipped with a warning, and the exit status is still 0. It
# makes a missing destination directory but not its parent (that is
# --mkpath, which an rsync before 3.2.3 does not know, on either end, and
# which is refused here). New files get the sender's mode under the far
# side's umask (077 here), unless --perms: then --chmod's.
RSYNC = """#!/usr/bin/env python3
import os, shutil, stat, sys
args = [a for a in sys.argv[1:] if not a.startswith("--partial-dir=") and a not in ("-q",)]
if "--mkpath" in args:
    sys.exit("rsync: --mkpath: unknown option")
recursive = "-r" in args
perms = "--perms" in args
modes = {}
for a in args:
    if a.startswith("--chmod="):
        for m in a[len("--chmod="):].split(","):
            modes[m[0]] = int(m[1:], 8)
src, dst = [a for a in args if not a.startswith("-")]
local = lambda p: p.split(":", 1)[1] if ":" in p else p
if ":" not in src + dst:
    sys.exit("rsync stub: neither end is a host:path")
src, dst = local(src), local(dst)
if not os.path.lexists(src):
    print('rsync: link_stat "%s" failed: No such file or directory (2)' % src, file=sys.stderr)
    sys.exit(23)
if os.path.islink(src):
    print('skipping non-regular file "%s"' % src, file=sys.stderr)
    sys.exit(0)
def mode(path, kind):
    if perms and kind in modes:
        return modes[kind]
    return stat.S_IMODE(os.stat(path).st_mode) & ~0o077
def copy(s, d):
    if os.path.isdir(s):
        if not recursive:
            sys.exit("rsync stub: skipping directory %s" % s)
        if not os.path.isdir(d):
            os.mkdir(d)
        os.chmod(d, mode(s, "D"))
        for n in os.listdir(s):
            copy(os.path.join(s, n), os.path.join(d, n))
    else:
        shutil.copyfile(s, d + ".tmp")
        os.chmod(d + ".tmp", mode(s, "F"))
        os.rename(d + ".tmp", d)
if dst.endswith("/") or os.path.isdir(dst):
    if not os.path.isdir(os.path.dirname(dst.rstrip("/"))):
        sys.exit("rsync: mkdir %s failed: No such file or directory (2)" % dst)
    os.makedirs(dst, exist_ok=True)
    dst = dst if src.endswith("/") else os.path.join(dst, os.path.basename(src))
elif not os.path.isdir(os.path.dirname(dst)):
    sys.exit("rsync: change_dir#3 %s failed: No such file or directory (2)" % os.path.dirname(dst))
copy(src.rstrip("/") if os.path.isdir(src) else src, dst.rstrip("/"))
"""


# An OpenSSL 1.1.1 host (Ubuntu 20.04): no cms -no-CAstore.
OLD_OPENSSL = """#!/bin/sh
[ "$1" = version ] && { echo "OpenSSL 1.1.1f  31 Mar 2020"; exit 0; }
for a; do [ "$a" = -no-CAstore ] && { echo "cms: Unknown option: -no-CAstore" >&2; echo "cms: Use -help for summary." >&2; exit 1; }; done
exec %s "$@"
""" % shutil.which("openssl")


def publish(env, channel, bundle, cp_stub=None, umask=None, stubs=None, args=(), wait=True):
    """stubs: {command: script} put first on PATH for this run."""
    env = dict(env)
    stub = os.path.join(env["WORK"], "stub")
    os.makedirs(stub, exist_ok=True)
    texts = {"cp": cp_stub, "rsync": RSYNC, "openssl": None, "date": None}
    texts.update(stubs or {})
    for name, text in texts.items():
        p = os.path.join(stub, name)
        if os.path.exists(p):
            os.unlink(p)
        if text:
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
    env["PATH"] = stub + ":" + env["PATH"]
    old = os.umask(umask) if umask is not None else None
    try:
        p = subprocess.Popen(["bash", PUBLISH] + list(args) + [channel, bundle],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    finally:
        if old is not None:
            os.umask(old)
    if not wait:
        return p
    out = p.communicate()[0]
    return p.returncode, out


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ssh as OpenSSH parses its options; the command runs in the fake board
# (absolute paths moved under it), as the user the target names.
SSH = """#!/bin/sh
log=%(board)s/ssh.log
echo "ssh $*" >> "$log"
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|-i|-p|-J|-F|-l) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
target="$1"; shift
case "$target" in *@*) user="${target%%%%@*}" ;; *) user=root ;; esac
echo "$user" > %(board)s/user
cmd=$(printf '%%s' "$*" | sed -e 's|/data/|%(board)s/data/|g' -e 's|/proc/|%(board)s/proc/|g')
PATH=%(board)s/bin:$PATH exec sh -c "$cmd"
"""
# scp, as OpenSSH's: -p preserves times, the port is -P.
SCP = """#!/bin/sh
echo "scp $*" >> %(board)s/ssh.log
while [ "$#" -gt 2 ]; do
    case "$1" in -o|-i|-P|-l|-J|-F) shift 2 ;; -*) shift ;; *) echo "scp: $1: No such file or directory" >&2; exit 1 ;; esac
done
src="$1"; dst="${2#*:}"
cp "$src" "$(printf '%%s' "$dst" | sed 's|^/data/|%(board)s/data/|')"
"""
BOARD_BIN = {
    "id": """#!/bin/sh
if [ "$(cat %(board)s/user)" = root ] || [ -n "${SUDO_DONE:-}" ]; then echo 0; else echo 1000; fi
""",
    "sudo": """#!/bin/sh
[ "$1" = -n ] && shift
[ "$(cat %(board)s/user)" = boat ] || { echo "sudo: a password is required" >&2; exit 1; }
SUDO_DONE=1 exec "$@"
""",
    # boat-update: status for anyone; install needs root, records what it
    # installed, and with --reboot "reboots" - the ssh session drops (255).
    "boat-update": """#!/bin/sh
case "$1" in
    status) echo "slot $(sed 's/.*rauc.slot=//' %(board)s/proc/cmdline)"; exit 0 ;;
    install)
        if [ "$(cat %(board)s/user)" != root ] && [ -z "${SUDO_DONE:-}" ]; then
            echo "boat-update: run as root" >&2; exit 1
        fi
        [ -e %(board)s/refuse ] && { echo "boat-update: refused" >&2; exit 1; }
        sha256sum < "$2" > %(board)s/installed
        case " $* " in
            *" --reboot "*)
                echo rauc.slot=B > %(board)s/proc/cmdline
                echo second-boot > %(board)s/proc/sys/kernel/random/boot_id
                exit 255 ;;
        esac ;;
esac
""",
}


def fake_board(work, name):
    board = os.path.join(work, name)
    for d in ("bin", "stub", "data/boat-update", "proc/sys/kernel/random"):
        os.makedirs(os.path.join(board, d))
    for rel, text in (("proc/cmdline", "rauc.slot=A\n"),
                      ("proc/sys/kernel/random/boot_id", "first-boot\n"), ("user", "root\n")):
        with open(os.path.join(board, rel), "w") as f:
            f.write(text)
    for d, scripts in (("stub", {"ssh": SSH, "scp": SCP,
                                 "ping": "#!/bin/sh\nexit 1\n", "sleep": "#!/bin/sh\nexit 0\n"}),
                       ("bin", BOARD_BIN)):
        for n, text in scripts.items():
            path = os.path.join(board, d, n)
            with open(path, "w") as f:
                f.write(text % dict(board=board))
            os.chmod(path, 0o755)
    return board


def push(board, bundle, args=(), **env):
    """push-update.sh against the fake board; ping never answers (a jump
    host), so a wait on it would time out."""
    e = dict(os.environ, PATH=os.path.join(board, "stub") + ":" + os.environ["PATH"],
             BOAT_HOST="boat-nx", BOAT_SSH_USER="root", BOAT_SSH_OPTS="")
    e.update(env)
    try:
        p = subprocess.run(["bash", PUSH] + list(args) + [bundle], capture_output=True, text=True,
                           env=e, timeout=120)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as t:
        return "timeout", (t.stdout or b"").decode() + (t.stderr or b"").decode()


def incoming(board):
    d = os.path.join(board, "data/boat-update/incoming")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def load_tool():
    spec = importlib.util.spec_from_file_location("boat_update", TOOL)
    bu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bu)
    return bu


class Board:
    """A board's view of channel indexes, through boat-update's own checks:
    validate_index, then decide, recording what it accepted the way
    check_channel does."""

    def __init__(self, bu, channel, booted, other="", firmware=None):
        self.bu, self.channel, self.booted, self.other = bu, channel, booted, other
        self.firmware = firmware
        self.state = {}

    def check(self, idx):
        """(offered version or None, message) - or ("error", why)."""
        idx = json.loads(json.dumps(idx))
        try:
            self.bu.validate_index(idx)
            offer, msg = self.bu.decide(idx, {"BOAT_UPDATE_CHANNEL": self.channel}, self.state,
                                        self.booted, self.other, "boat-xavier-nx", "35.6.4", set(),
                                        self.firmware)
        except self.bu.UpdateError as e:
            return "error", str(e)
        self.state.setdefault("serials", {})[idx["channel"]] = idx["serial"]
        self.state.setdefault("index_hashes", {})[idx["channel"]] = self.bu.index_hash(idx)
        return (offer["version"] if offer else None), msg


def keyring_check():
    """boat_keyring_certs from rauc_%.bbappend, with bb.fatal raising."""
    text = open(RAUC_APPEND).read()
    m = re.search(r"^def boat_keyring_certs\(.*?(?=^\S)", text, re.S | re.M)
    assert m, "rauc_%.bbappend has no boat_keyring_certs - update the test"

    class Fatal(Exception):
        pass

    class bb:
        @staticmethod
        def fatal(msg):
            raise Fatal(msg)
    ns = {"bb": bb}
    exec(m.group(0), ns)

    def check(path):
        try:
            return ns["boat_keyring_certs"](path, "the keyring")
        except Fatal as e:
            return str(e)
    return check


def index(dest, channel, ca):
    p7 = os.path.join(dest, channel, "index.p7")
    if not os.path.exists(p7):
        return None
    out = subprocess.run(["openssl", "cms", "-verify", "-inform", "DER", "-binary", "-in", p7,
                          "-CAfile", ca, "-no-CApath", "-purpose", "smimesign", "-no_check_time"],
                         capture_output=True)
    return json.loads(out.stdout) if out.returncode == 0 else "unverified"


def main():
    if not shutil.which("openssl"):
        print("SKIP: openssl not installed")
        return 77
    work = tempfile.mkdtemp(prefix="publish-test-")
    try:
        fleet, bench = os.path.join(work, "fleet-keys"), os.path.join(work, "bench-keys")
        env = dict(os.environ, BOAT_KEYS_DIR=fleet, BOAT_BENCH_KEYS_DIR=bench, WORK=work)
        for args in ([], ["--bench"]):
            subprocess.run(["bash", GEN] + args, check=True, capture_output=True, env=env)
        n = [0]

        def dest():
            n[0] += 1
            d = os.path.join(work, "www%d" % n[0])
            env["BOAT_PUBLISH_DEST"] = d
            return d

        def bundle(**kw):
            d = os.path.join(work, "b%d" % len(os.listdir(work)))
            os.makedirs(d)
            return make_bundle(os.path.join(d, "x.raucb"), kw.pop("keys", fleet), **kw)

        d = dest()
        good = bundle()
        good_sha = hashlib.sha256(open(good, "rb").read()).hexdigest()
        rc, out = publish(env, "stable", good)
        idx = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        named = os.path.join(d, "stable", idx["bundle"]) if isinstance(idx, dict) else ""
        check("a good bundle is published: a signed index naming it, with its size and SHA-256, "
              "and the bundle whole",
              rc == 0 and isinstance(idx, dict) and idx["version"] == "1.2.0"
              and idx["sha256"] == good_sha and idx["size"] == os.path.getsize(good)
              and idx["bundle"] == "boat-bundle-1.2.0-%s.raucb" % good_sha[:12]
              and idx.get("firmware") == "f" * 64
              and os.path.exists(named) and open(named, "rb").read() == open(good, "rb").read()
              and not [f for f in os.listdir(os.path.join(d, "stable")) if f.endswith(".tmp")],
              "%d %s %s" % (rc, idx, out))
        bu = load_tool()
        board = Board(bu, "stable", "1.1.0")
        check("... and a board running 1.1.0 takes the published index as it is (boat-update's checks)",
              isinstance(idx, dict) and board.check(idx)[0] == "1.2.0", board.check(idx) if isinstance(idx, dict) else idx)
        first = idx["serial"] if isinstance(idx, dict) else 0
        rc, out = publish(env, "stable", good)
        idx = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        check("... and published again, its serial grows",
              rc == 0 and isinstance(idx, dict) and idx["serial"] > first, out)

        for name, b, why in (
                ("a plain bundle (detached signature)", bundle(detached=True), "verity"),
                ("a bundle signed under another CA", bundle(keys=bench), "does not verify"),
                ("a bundle signed by the index key, not the bundle key", bundle(signer="index-signing"),
                 "codeSigning"),
                ("a bundle whose signed manifest says format=plain", bundle(fmt="plain"), "verity format"),
                ("a bench build's bundle", bundle(keys=bench, compatible="boat-xavier-nx-bench"),
                 "does not verify|bench")):
            d = dest()
            with open(os.path.join(work, "notabundle"), "wb") as f:
                f.write(b"\0" * 100)
            rc, out = publish(env, "stable", b)
            check("%s is not published" % name,
                  rc != 0 and index(d, "stable", os.path.join(fleet, "ca.cert.pem")) is None
                  and any(w in out for w in why.split("|")), "%d %s" % (rc, out))
        d = dest()
        rc, out = publish(env, "stable", os.path.join(work, "notabundle"))
        check("a file that is not a bundle is not published",
              rc != 0 and index(d, "stable", os.path.join(fleet, "ca.cert.pem")) is None, out)

        d = dest()
        bb = bundle(keys=bench, compatible="boat-xavier-nx-bench")
        rc, out = publish(env, "bench", bb)
        idx = index(d, "bench", os.path.join(bench, "ca.cert.pem"))
        check("a bench bundle goes to the bench channel, its index signed with the bench keys",
              rc == 0 and isinstance(idx, dict) and idx["compatible"] == "boat-xavier-nx-bench"
              and index(d, "bench", os.path.join(fleet, "ca.cert.pem")) == "unverified"
              and os.path.exists(os.path.join(bench, "index-serial")), "%d %s %s" % (rc, idx, out))

        # The deploy directory's link, to a host:path destination (rsync).
        d = dest()
        link = os.path.join(work, "boat-bundle-jetson-xavier-nx-devkit.raucb")
        os.symlink(good, link)
        env["BOAT_PUBLISH_DEST"] = "buildhost:" + d
        rc, out = publish(env, "stable", link)
        idx = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        named = os.path.join(d, "stable", idx["bundle"]) if isinstance(idx, dict) else ""
        check("the deploy directory's link, published over rsync, uploads the bundle itself",
              rc == 0 and isinstance(idx, dict) and os.path.exists(named) and not os.path.islink(named)
              and open(named, "rb").read() == open(good, "rb").read(), "%d %s %s" % (rc, idx, out))

        # The publisher's umask is not the web server's business.
        for how in ("local", "rsync"):
            d = dest()
            if how == "rsync":
                env["BOAT_PUBLISH_DEST"] = "buildhost:" + d
            rc, out = publish(env, "stable", good, umask=0o077)
            ch = os.path.join(d, "stable")
            files = sorted(os.listdir(ch)) if os.path.isdir(ch) else []
            check("under umask 077 (%s), the index and bundle are 0644 and the directories 0755" % how,
                  rc == 0 and len(files) == 2 and all(mode(os.path.join(ch, f)) == 0o644 for f in files)
                  and mode(ch) == 0o755 and mode(d) == 0o755,
                  "%d %s %s" % (rc, [(f, oct(mode(os.path.join(ch, f)))) for f in files], out))

        d = dest()
        rc, out = publish(env, "stable", good, stubs={"openssl": OLD_OPENSSL})
        check("an OpenSSL 1.1 host is told so, not that the bundle is bad",
              rc != 0 and "OpenSSL 3.0 or newer" in out and "does not verify" not in out
              and index(d, "stable", os.path.join(fleet, "ca.cert.pem")) is None, "%d %s" % (rc, out))

        # Fleet flows, through a board's own checks.
        d = dest()
        rc1, out1 = publish(env, "testing", good)
        rc2, out2 = publish(env, "stable", good)
        t = index(d, "testing", os.path.join(fleet, "ca.cert.pem"))
        st = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        ok = rc1 == 0 and rc2 == 0 and isinstance(t, dict) and isinstance(st, dict)
        check("promotion: testing then stable name the same file, and the serial grows across channels",
              ok and t["bundle"] == st["bundle"] and t["sha256"] == st["sha256"] == good_sha
              and st["serial"] > t["serial"]
              and sorted(os.listdir(os.path.join(d, "stable"))) == sorted(os.listdir(os.path.join(d, "testing"))),
              "%s %s %s %s" % (t, st, out1, out2))
        board = Board(bu, "testing", "1.1.0")
        first = board.check(t) if ok else ("error", "not published")
        board.channel = "stable"
        second = board.check(st) if ok else ("error", "not published")
        check("... a board that took it on testing, then follows stable, is offered it there too",
              first[0] == "1.2.0" and second[0] == "1.2.0", "%s %s" % (first, second))

        # The same version rebuilt: other bytes, another name - never the tried file's.
        d = dest()
        rebuilt = bundle()
        publish(env, "stable", good)
        old = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        rc, out = publish(env, "stable", rebuilt)
        new = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        check("a version rebuilt gets another name, and the file published before stays as it was",
              rc == 0 and isinstance(old, dict) and isinstance(new, dict) and new["bundle"] != old["bundle"]
              and new["serial"] > old["serial"]
              and open(os.path.join(d, "stable", old["bundle"]), "rb").read() == open(good, "rb").read(),
              "%d %s %s %s" % (rc, old, new, out))

        # Withdrawal: an older release published over a newer one.
        d = dest()
        newer = bundle(version="1.3.0")
        publish(env, "stable", newer)
        idx13 = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        board = Board(bu, "stable", "1.2.0")
        took = board.check(idx13) if isinstance(idx13, dict) else ("error", "not published")
        rc, out = publish(env, "stable", good)
        idx12 = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        after = board.check(idx12) if isinstance(idx12, dict) else ("error", "not published")
        check("withdrawal: 1.2.0 published over 1.3.0 gets a higher serial, and a board offered 1.3.0 "
              "is offered nothing",
              took[0] == "1.3.0" and rc == 0 and idx12["serial"] > idx13["serial"]
              and after[0] is None and after[1].startswith("up to date"), "%s %s %s" % (took, after, out))
        check("... and the older index served again (a stale cache) is refused as a replay",
              board.check(idx13)[0] == "error" and "replayed" in board.check(idx13)[1], board.check(idx13))

        # A second publishing machine: its own counter far behind, its clock
        # an hour behind. The channel's index is what it must stay above.
        for how in ("local", "rsync"):
            d = dest()
            if how == "rsync":
                env["BOAT_PUBLISH_DEST"] = "buildhost:" + d
            publish(env, "stable", good)
            before = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
            other = os.path.join(work, "other-keys-%s" % how)
            shutil.copytree(fleet, other)
            with open(os.path.join(other, "index-serial"), "w") as f:
                f.write("5\n")
            lagging = "#!/bin/sh\n[ \"$1\" = +%%s ] && { echo %d; exit 0; }\nexec /bin/date \"$@\"\n" % (
                time.time() - 3600)
            rc, out = publish(dict(env, BOAT_KEYS_DIR=other), "stable", newer, stubs={"date": lagging})
            after = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
            board = Board(bu, "stable", "1.1.0")
            if isinstance(before, dict):
                board.check(before)
            got = board.check(after) if isinstance(after, dict) else ("error", "not published")
            check("a second publisher (%s) with a stale counter and a clock behind still publishes above "
                  "the channel's serial" % how,
                  rc == 0 and isinstance(before, dict) and after["serial"] > before["serial"] and got[0] == "1.3.0",
                  "%d %s %s %s %s" % (rc, before, after, got, out))
        env["BOAT_PUBLISH_DEST"] = d

        # The channel's index cannot be read back (the host unreachable): no
        # publish, rather than one below the boards' serial or past a device
        # tree change. A channel with no index yet still publishes (above).
        d = dest()
        env["BOAT_PUBLISH_DEST"] = "buildhost:" + d
        publish(env, "stable", good)
        before = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        unreachable = ("#!/bin/sh\nfor a; do case \"$a\" in *:*index.p7) "
                       "echo 'ssh: connect to host buildhost port 22: Connection refused' >&2; exit 255 ;; esac; done\n"
                       "exec %s/stub-rsync \"$@\"\n" % work)
        with open(os.path.join(work, "stub-rsync"), "w") as f:
            f.write(RSYNC)
        os.chmod(os.path.join(work, "stub-rsync"), 0o755)
        rc, out = publish(env, "stable", newer, stubs={"rsync": unreachable})
        after = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        check("an index that cannot be read back stops the publish, and the channel keeps its index",
              rc != 0 and "could not read back" in out and isinstance(before, dict) and after == before,
              "%d %s" % (rc, out))
        env["BOAT_PUBLISH_DEST"] = d

        # Two publishes at once must not share a serial: the second waits.
        d = dest()
        with open(os.path.join(fleet, "index-serial.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.time()
            timer = threading.Timer(2, lambda: fcntl.flock(lock, fcntl.LOCK_UN))
            timer.start()
            rc, out = publish(env, "stable", good)
            took = time.time() - started
            timer.join()
        check("a publish waits for another one holding the serial lock",
              rc == 0 and took >= 2, "%d after %.1fs: %s" % (rc, took, out))

        # Built against another device tree than the channel's release.
        d = dest()
        publish(env, "stable", good)
        before = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        other_dtb = bundle(version="1.3.0", firmware="e" * 64)
        rc, out = publish(env, "stable", other_dtb)
        check("a bundle built against another device tree than the channel's release is not published",
              rc != 0 and "another kernel device tree" in out
              and index(d, "stable", os.path.join(fleet, "ca.cert.pem")) == before, "%d %s" % (rc, out))
        rc, out = publish(env, "stable", other_dtb, args=["--reflash"])
        idx = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        check("... unless it is published as a reflash",
              rc == 0 and isinstance(idx, dict) and idx["requires_reflash"] is True
              and idx.get("firmware") == "e" * 64, "%d %s %s" % (rc, idx, out))
        if isinstance(idx, dict):
            got = Board(bu, "stable", "1.2.0", firmware="f" * 64).check(idx)
            check("... which a board flashed with the old device tree is told to reflash for, not offered",
                  got[0] is None and "another kernel device tree" in got[1] and "reflash at the dock" in got[1],
                  got)
            got = Board(bu, "stable", "1.2.0", firmware="e" * 64).check(idx)
            check("... and a board already flashed with the new one takes over the air", got[0] == "1.3.0", got)

        # A keyring that holds a private key: publish refuses, and so does the image build.
        bad = os.path.join(work, "bad-keys")
        shutil.copytree(fleet, bad)
        with open(os.path.join(bad, "ca.cert.pem"), "a") as f:
            f.write(open(os.path.join(fleet, "ca.key.pem")).read())
        d = dest()
        rc, out = publish(dict(env, BOAT_KEYS_DIR=bad), "stable", good)
        check("a keyring holding a private key is refused",
              rc != 0 and "PRIVATE KEY" in out, "%d %s" % (rc, out))
        keyring = keyring_check()
        got = keyring(os.path.join(fleet, "ca.cert.pem"))
        check("image keyring check: the update CA passes", isinstance(got, list) and len(got) == 1, got)
        got = keyring(os.path.join(bad, "ca.cert.pem"))
        check("image keyring check: a private key in it stops the build",
              isinstance(got, str) and "PRIVATE KEY" in got, got)
        got = keyring(os.path.join(fleet, "rauc-signing.cert.pem"))
        check("image keyring check: a signing certificate (not a CA) stops the build",
              isinstance(got, str) and "not a CA" in got, got)
        reencoded = os.path.join(work, "reencoded.pem")
        with open(reencoded, "w") as f:
            f.write(open(os.path.join(fleet, "ca.cert.pem")).read().replace("\n", "\r\n") + "\n")
        got = keyring(reencoded)
        check("image keyring check: the same CA re-encoded is the same certificate (bench vs fleet)",
              isinstance(got, list) and got == keyring(os.path.join(fleet, "ca.cert.pem")), got)

        # push-update.sh: the no-internet path.
        pb = os.path.join(work, "push", "boat-bundle-1.2.0-test.raucb")
        os.makedirs(os.path.dirname(pb))
        shutil.copyfile(good, pb)
        pb_sha = hashlib.sha256(open(pb, "rb").read()).hexdigest()

        def installed(board):
            p = os.path.join(board, "installed")
            return open(p).read().split()[0] if os.path.exists(p) else None

        def ssh_log(board):
            return open(os.path.join(board, "ssh.log")).read()

        board = fake_board(work, "board-port")
        rc, out = push(board, pb, BOAT_SSH_OPTS="-i /k/boat_ed25519 -p 2222")
        check("push-update: BOAT_SSH_OPTS with -p PORT (ssh's option) installs the bundle",
              rc == 0 and installed(board) == pb_sha and "scp" not in ssh_log(board), "%s %s" % (rc, out))
        check("push-update: ssh sessions keep alive (ServerAliveInterval)",
              "ServerAliveInterval=15" in ssh_log(board), ssh_log(board))

        board = fake_board(work, "board-jump")
        rc, out = push(board, pb, BOAT_SSH_OPTS="-J gw.boat")
        check("push-update: the reboot is seen over SSH (a new boot ID) - through a jump host ping "
              "never answers", rc == 0 and "came back on slot B" in out, "%s %s" % (rc, out))
        check("push-update: the copy on /data is gone after the install", incoming(board) == [],
              incoming(board))

        board = fake_board(work, "board-boat-user")
        rc, out = push(board, pb, BOAT_SSH_USER="boat")
        check("push-update: BOAT_SSH_USER=boat installs through its passwordless sudo",
              rc == 0 and installed(board) == pb_sha and incoming(board) == [], "%s %s" % (rc, out))

        board = fake_board(work, "board-no-reboot")
        os.makedirs(os.path.join(board, "data/boat-update/incoming"))
        old = os.path.join(board, "data/boat-update/incoming/old-1.1.0.raucb")
        with open(old, "w") as f:
            f.write("a copy an earlier run left")
        day_ago = time.time() - 2 * 86400
        os.utime(old, (day_ago, day_ago))
        with open(os.path.join(board, "data/boat-update/incoming/other-1.2.0.raucb.part"), "w") as f:
            f.write("another push, copying now")
        rc, out = push(board, pb, args=["--no-reboot"])
        check("push-update --no-reboot installs and leaves no bundle of its own on /data, nor one an "
              "earlier run left a day ago - but another push's copy in progress stays",
              rc == 0 and installed(board) == pb_sha and incoming(board) == ["other-1.2.0.raucb.part"],
              "%s %s %s" % (rc, incoming(board), out))

        board = fake_board(work, "board-refuses")
        with open(os.path.join(board, "refuse"), "w"):
            pass
        rc, out = push(board, pb)
        check("push-update: an install the board refuses fails, and leaves no bundle on /data",
              rc not in (0, "timeout") and "the install failed" in out and incoming(board) == [],
              "%s %s %s" % (rc, incoming(board), out))

        failing = "#!/bin/sh\ncase \"$2\" in *.raucb.tmp) echo 'cp: No space left on device' >&2; exit 1 ;; esac\nexec /bin/cp \"$@\"\n"
        short = ("#!/bin/sh\ncase \"$2\" in *.raucb.tmp) head -c 1000 \"$1\" > \"$2\"; exit 0 ;; esac\n"
                 "exec /bin/cp \"$@\"\n")
        for name, stub in (("fails", failing), ("leaves a short file", short)):
            d = dest()
            rc, out = publish(env, "stable", good, cp_stub=stub)
            check("a bundle copy that %s publishes no index" % name,
                  rc != 0 and index(d, "stable", os.path.join(fleet, "ca.cert.pem")) is None
                  and "nothing published" in out, "%d %s" % (rc, out))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d check(s) failed" % len(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
