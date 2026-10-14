#!/usr/bin/env python3
"""Run publish-update.sh, as shipped, against a local channel directory.

    scripts/test-publish-update.py      # needs openssl

publish-update.sh decides what every boat following a channel is offered.
This makes real update keys with scripts/gen-update-keys.sh (the fleet's
and the bench's), bundles laid out the way RAUC 1.13 lays out a verity
bundle (payload, then a CMS signature that carries the manifest, then the
signature's size), and publishes them to a scratch directory. It checks
that a good bundle is published - the index signed, naming the bundle,
which is there whole - and that a bundle a board's RAUC would refuse, a
bench bundle on a fleet channel, or a copy that fails half way publishes
nothing.

Exit status: 0 = all passed, 1 = a case failed, 77 = openssl missing.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLISH = os.path.join(HERE, "publish-update.sh")
GEN = os.path.join(HERE, "gen-update-keys.sh")

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok:  " if ok else "FAIL:", name, "" if ok else "\n    " + detail))
    if not ok:
        failures.append(name)


def openssl(*args):
    return subprocess.run(["openssl"] + list(args), check=True, capture_output=True)


def make_bundle(path, keys, signer="rauc-signing", compatible="boat-xavier-nx", version="1.2.0",
                fmt="verity", detached=False):
    """A bundle as RAUC 1.13 writes a verity one: the payload, the CMS
    signature carrying the manifest, then the signature's size."""
    work = os.path.dirname(path)
    manifest = os.path.join(work, "manifest.raucm")
    with open(manifest, "w") as f:
        f.write("[update]\ncompatible=%s\nversion=%s\n\n[bundle]\nformat=%s\nverity-hash=00\n\n"
                "[meta.boat]\nl4t=35.6.4\n" % (compatible, version, fmt))
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


# rsync to a host:path destination, as rsync behaves without -l/-L/-a: a
# symlink is skipped with a warning, and the exit status is still 0.
RSYNC = """#!/bin/sh
for a; do src="$dst"; dst="$a"; done
case "$dst" in *:*) ;; *) echo "rsync stub: not a host:path" >&2; exit 1 ;; esac
if [ -L "$src" ]; then echo "skipping non-regular file \"$src\"" >&2; exit 0; fi
dst="${dst#*:}"
mkdir -p "$(dirname "$dst")" && exec /bin/cp "$src" "$dst"
"""


def publish(env, channel, bundle, cp_stub=None):
    env = dict(env)
    stub = os.path.join(env["WORK"], "stub")
    os.makedirs(stub, exist_ok=True)
    for name, text in (("cp", cp_stub), ("rsync", RSYNC)):
        p = os.path.join(stub, name)
        if os.path.exists(p):
            os.unlink(p)
        if text:
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
    env["PATH"] = stub + ":" + env["PATH"]
    p = subprocess.run(["bash", PUBLISH, channel, bundle], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


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
        rc, out = publish(env, "stable", good)
        idx = index(d, "stable", os.path.join(fleet, "ca.cert.pem"))
        named = os.path.join(d, "stable", idx["bundle"]) if isinstance(idx, dict) else ""
        check("a good bundle is published: a signed index naming it, and the bundle whole",
              rc == 0 and isinstance(idx, dict) and idx["version"] == "1.2.0"
              and os.path.exists(named) and open(named, "rb").read() == open(good, "rb").read()
              and not [f for f in os.listdir(os.path.join(d, "stable")) if f.endswith(".tmp")],
              "%d %s %s" % (rc, idx, out))
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
