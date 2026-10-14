#!/usr/bin/env python3
"""Exercise boat-update against a real HTTPS update channel on localhost.

    scripts/test-boat-update.py       # needs openssl, curl, grub-editenv

boat-update decides, from a file anyone on the internet could serve, what
gets written into the boat's other slot. This runs it, as shipped, against:

  - a throwaway update CA, made here the same way scripts/gen-update-keys.sh
    makes the real one (an index-signing leaf with emailProtection, a bundle
    signer with codeSigning), plus a second, unrelated CA;
  - a local HTTPS server with Range support that can also cut a download
    off half way, the way a dropped link does - its certificate from a TLS
    CA of its own, which the board trusts only through server-ca.pem, as
    a bench channel's is;
  - a fake board (cmdline, mounts, grubenv, /data), with rauc, nmcli, ip,
    chronyc and blkid stubbed.

It checks that a good index is accepted and its bundle downloaded - across
an interrupted transfer - and verified; and that an index signed by
another CA, signed with the bundle key, replayed, or published for another
channel is refused before anything is downloaded. systemd (systemctl,
systemd-run, systemd-inhibit, journalctl) is stubbed too, so the install
wrapper, the timer's auto run, rollback and the locking are run as well. A misbehaving host gets
its own cases: an endless chunked index or bundle (cut off at the signed
size as it arrives - kirkstone's curl 7.82 would not), and a server that
ignores Range. So do give-ups, per-channel serials, a stale ready bundle and
a trial that died before boat-commit, and that a refused check or
download shows in status as an alert until it works again. It also checks the pure parts
(version order as RAUC defines it, index validation) directly.

Exit status: 0 = all passed, 1 = a case failed, 77 = a tool is missing.
"""
import contextlib
import fcntl
import hashlib
import io
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "layers", "meta-boat", "external", "rauc",
                    "recipes-boat", "boat-update", "files", "boat-update.py")
DISK = "/dev/nvme0n1"
COMPATIBLE = "boat-xavier-nx"

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok:  " if ok else "FAIL:", name, "" if ok else "\n    " + detail))
    if not ok:
        failures.append(name)


def openssl(*args, **kw):
    subprocess.run(["openssl"] + list(args), check=True, capture_output=True, **kw)


def make_ca(d, name):
    openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", d + "/%s.key" % name)
    openssl("req", "-new", "-x509", "-key", d + "/%s.key" % name, "-out", d + "/%s.pem" % name,
            "-days", "30", "-subj", "/CN=%s" % name,
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")


def make_leaf(d, ca, name, eku, cn=None, san=None):
    openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", d + "/%s.key" % name)
    openssl("req", "-new", "-key", d + "/%s.key" % name, "-out", d + "/%s.csr" % name, "-subj", "/CN=%s" % (cn or name))
    ext = "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\n"
    if eku:
        ext += "extendedKeyUsage=%s\n" % eku
    if san:
        ext += "subjectAltName=%s\n" % san
    with open(d + "/%s.ext" % name, "w") as f:
        f.write(ext)
    openssl("x509", "-req", "-in", d + "/%s.csr" % name, "-out", d + "/%s.pem" % name,
            "-CA", d + "/%s.pem" % ca, "-CAkey", d + "/%s.key" % ca, "-CAcreateserial",
            "-days", "30", "-extfile", d + "/%s.ext" % name)


def sign_index(d, doc, signer, out):
    with open(d + "/index.json", "w") as f:
        json.dump(doc, f)
    openssl("cms", "-sign", "-nodetach", "-binary", "-outform", "DER", "-in", d + "/index.json",
            "-signer", d + "/%s.pem" % signer, "-inkey", d + "/%s.key" % signer, "-out", out)


class Channel(http.server.SimpleHTTPRequestHandler):
    """Static files with Range support; cut_after makes the next GET of a
    bundle stop after that many bytes, like a link that drops. mode makes it
    misbehave: "chunked-endless" (the index) and "chunked-oversize" (a
    bundle) send a chunked body far past the file - no Content-Length for
    curl to check - "ignore-range" answers a Range request with 200,
    "refuse-range" with 416, "redirect" sends a bundle request somewhere
    else, and "shifted-range" and "short-range" answer a bundle's Range
    request with a 206 for other offsets or fewer bytes."""
    cut_after = None
    root = None
    mode = None
    protocol_version = "HTTP/1.1"

    def flood(self):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        block = os.urandom(1 << 16)
        try:
            for _ in range(128):                       # 8 MiB, then give up
                self.wfile.write(b"%x\r\n%s\r\n" % (len(block), block))
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass

    def translate_path(self, p):
        return os.path.join(self.root, p.lstrip("/").split("?")[0])

    def log_message(self, *a):
        pass

    def do_GET(self):
        fpath = self.translate_path(self.path)
        if not os.path.isfile(fpath):
            self.send_error(404)
            return
        if Channel.mode == "redirect" and fpath.endswith(".raucb"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere.raucb")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if (Channel.mode == "chunked-endless" and fpath.endswith("index.p7")) or \
                (Channel.mode == "chunked-oversize" and fpath.endswith(".raucb")):
            self.flood()
            return
        size = os.path.getsize(fpath)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and Channel.mode != "ignore-range":
            first, last = rng.split("=")[1].split("-")
            start = int(first)
            if last:
                end = min(int(last), size - 1)
            if start >= size or (Channel.mode == "refuse-range" and fpath.endswith(".raucb")):
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            if fpath.endswith(".raucb") and Channel.mode == "shifted-range":
                start, end = start + 1, min(end + 1, size - 1)
            elif fpath.endswith(".raucb") and Channel.mode == "short-range":
                end -= 1
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end + 1 - start))
        self.end_headers()
        with open(fpath, "rb") as f:
            f.seek(start)
            data = f.read(end + 1 - start)
        if fpath.endswith(".raucb") and Channel.cut_after is not None:
            data = data[:Channel.cut_after]
            Channel.cut_after = None
            self.wfile.write(data)
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass                     # the client gave up (e.g. on ignore-range)


def write(p, text, mode=0o644):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)


def main():
    for tool in ("openssl", "curl", "grub-editenv"):
        if not shutil.which(tool):
            print("SKIP: %s not installed" % tool)
            return 77
    spec = importlib.util.spec_from_file_location("boat_update", TOOL)
    bu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bu)

    # --- pure parts ------------------------------------------------------
    order = ["0.0.0-dev", "0.9", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0-rc.10",
             "1.0.0-rc.a", "1.0.0", "1.0.1", "1.2", "2026.10.1", "2026.10.2"]
    ok = all(bu.newer(b, a) for a, b in zip(order, order[1:]))
    check("versions order the way RAUC's relaxed semver does", ok, " < ".join(order))
    check("a build suffix does not change the order",
          not bu.newer("1.0.0+abc", "1.0.0") and not bu.newer("1.0.0", "1.0.0+abc"))
    base = dict(channel="stable", serial=5, compatible=COMPATIBLE, version="1.1.0",
                l4t="35.6.4", bundle="boat-bundle-1.1.0-abc.raucb", size=10,
                sha256="0" * 64, published="2026-10-13T00:00:00Z")
    for field, value in (("bundle", "../etc/passwd.raucb"), ("bundle", "sub/x.raucb"),
                         ("bundle", "x.tar"), ("sha256", "ABC"), ("serial", "5"),
                         ("version", "one"), ("size", 0), ("requires_reflash", "yes")):
        idx = dict(base, **{field: value})
        try:
            bu.validate_index(idx)
            check("index with %s=%r is refused" % (field, value), False, "accepted")
        except bu.UpdateError:
            check("index with %s=%r is refused" % (field, value), True)

    work = tempfile.mkdtemp(prefix="boat-update-test-")
    try:
        keys = os.path.join(work, "keys")
        os.makedirs(keys)
        make_ca(keys, "ca")
        make_leaf(keys, "ca", "index", "emailProtection")
        make_leaf(keys, "ca", "bundlekey", "codeSigning")
        make_leaf(keys, "ca", "noeku", None)
        make_ca(keys, "otherca")
        make_leaf(keys, "otherca", "otherindex", "emailProtection")
        make_ca(keys, "tlsca")
        make_leaf(keys, "tlsca", "server", "serverAuth", cn="localhost", san="DNS:localhost,IP:127.0.0.1")

        # --- the channel -------------------------------------------------
        www = os.path.join(work, "www")
        chan = os.path.join(www, "boat-xavier-nx", "stable")
        os.makedirs(chan)
        bundle_bytes = os.urandom(3 * 1024 * 1024)
        sha = hashlib.sha256(bundle_bytes).hexdigest()
        bundle_name = "boat-bundle-1.1.0-%s.raucb" % sha[:12]
        with open(os.path.join(chan, bundle_name), "wb") as f:
            f.write(bundle_bytes)
        Channel.root = www
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Channel)
        # A client that hangs up mid-request is part of several cases;
        # Python 3.10's socketserver would print a traceback for each.
        httpd.handle_error = lambda request, client_address: None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(keys + "/server.pem", keys + "/server.key")
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = "https://localhost:%d/boat-xavier-nx" % httpd.server_address[1]
        # The channel's TLS certificate is from a CA of its own, like a
        # bench server's: the board trusts it only through server-ca.pem.
        for var in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
            os.environ.pop(var, None)

        def publish(serial, channel="stable", signer="index", version="1.1.0", where="stable", **extra):
            doc = dict(channel=channel, serial=serial, compatible=COMPATIBLE, version=version,
                       l4t="35.6.4", bundle=bundle_name, size=len(bundle_bytes), sha256=sha,
                       published="2026-10-13T00:00:00Z", notes="test release\x07")
            doc.update(extra)
            # where: the channel directory it is served from - normally
            # stable, which is what the board follows unless told otherwise.
            d = os.path.join(www, "boat-xavier-nx", where)
            os.makedirs(d, exist_ok=True)
            sign_index(keys, doc, signer, os.path.join(d, "index.p7"))

        # --- the board -------------------------------------------------
        board = os.path.join(work, "board")
        binp = os.path.join(board, "bin")
        os.makedirs(binp)
        write(board + "/proc/cmdline", "root=x rauc.slot=A\n")
        write(board + "/proc/mounts", "%sp1 / ext4 rw 0 0\n%sp18 %s/data ext4 rw 0 0\n" % (DISK, DISK, board))
        os.makedirs(board + "/data/boat-update")
        os.makedirs(board + "/boot/efi/EFI/BOOT")
        env = board + "/boot/efi/EFI/BOOT/grubenv"
        subprocess.run(["grub-editenv", env, "create"], check=True)
        subprocess.run(["grub-editenv", env, "set", "ORDER=A B", "A_OK=1", "A_TRY=0", "A_COMMITTED=1",
                        "B_OK=1", "B_TRY=0", "B_COMMITTED=1", "BOAT_STAY=0"], check=True)
        write(board + "/etc/boat-release", "1.0.0\n")
        write(board + "/etc/nv_tegra_release", "# R35 (release), REVISION: 6.4, GCID: 1\n")
        write(board + "/etc/rauc/system.conf", "[system]\ncompatible=%s\nbootloader=grub\n" % COMPATIBLE)
        shutil.copy(keys + "/ca.pem", board + "/etc/rauc/ca.cert.pem")
        write(board + "/etc/boat-update/channel.conf",
              'BOAT_UPDATE_URL="%s"\nBOAT_UPDATE_CHANNEL=stable\n' % url)
        write(board + "/metered", "no\n")
        write(binp + "/ip", "#!/bin/sh\necho 'default via 127.0.0.1 dev lo'\n", 0o755)
        write(board + "/nmtype", "loopback\n")
        write(binp + "/nmcli", "#!/bin/sh\nprintf '%%s\\n%%s\\n' \"$(cat %s/metered)\" \"$(cat %s/nmtype)\"\n"
              % (board, board), 0o755)
        write(board + "/leap", "Normal\n")
        write(binp + "/chronyc", "#!/bin/sh\necho \"Leap status     : $(cat %s/leap)\"\n" % board, 0o755)
        # systemd: what boat-update asks of it is logged, nothing is run.
        # The install unit "runs" by leaving unit-result as its Result.
        write(board + "/unit-result", "success\n")
        # rauc.service's cgroup is this test's own, so the stub RAUC's
        # streaming requests (curl, run from here) come from "rauc.service".
        own = {}
        for line in open("/proc/self/cgroup").read().splitlines():
            _, controllers, cgpath = line.split(":", 2)
            own.setdefault(controllers or "unified", cgpath)
        own_cgroup = own.get("unified") or own.get("name=systemd") or "/"
        write(board + "/rauc-cgroup", own_cgroup + "\n")
        write(board + "/rauc-journal", "")
        write(binp + "/systemctl", """#!/bin/sh
echo "systemctl $*" >> %(b)s/systemd.log
state=$(cat %(b)s/unit-state 2>/dev/null || echo inactive)
case "$1" in
    is-active) [ "$state" = active ] ;;      # as systemd: a running oneshot is "activating"
    show)
        case "$*" in
            *ActiveState*) echo "$state" ;;
            *ControlGroup*) cat %(b)s/rauc-cgroup ;;
            *) cat %(b)s/unit-result ;;
        esac ;;
    *) exit 0 ;;
esac
""" % dict(b=board), 0o755)
        write(binp + "/systemd-run", "#!/bin/sh\necho \"systemd-run $*\" >> %s/systemd.log\n" % board, 0o755)
        write(binp + "/systemd-inhibit", """#!/bin/sh
while [ "${1#--}" != "$1" ]; do shift; done
exec "$@"
""", 0o755)
        write(binp + "/journalctl", """#!/bin/sh
case " $* " in
    *" -u rauc "*) cat %(b)s/rauc-journal ;;
esac
""" % dict(b=board), 0o755)
        write(binp + "/blkid", "#!/bin/sh\nexit 2\n", 0o755)
        # RAUC: slot B holds 1.0.0; "info" accepts the test bundle. A
        # streamed "install" logs its arguments; then it fails with
        # rauc-error (LastError), or exits with rauc-exit as a RAUC that
        # went away does, or puts the release in rauc-serves into slot B
        # and activates it, as RAUC would.
        def rauc_status(b_version, b_status="ok"):
            return json.dumps({"slots": [
                {"rootfs.0": {"bootname": "A", "slot_status": {"bundle": {"version": "1.0.0"}, "status": "ok"}}},
                {"rootfs.1": {"bootname": "B", "slot_status": {"bundle": {"version": b_version},
                                                               "status": b_status}}}]})
        write(board + "/rauc-status", rauc_status("1.0.0"))
        write(board + "/rauc-error", "")
        write(board + "/rauc-exit", "0")
        write(binp + "/rauc", """#!/bin/sh
case "$1" in
    status) cat %(b)s/rauc-status ;;
    info)
        [ -e %(b)s/rauc-info-fails ] && { echo "signature verification failed" >&2; exit 1; }
        echo '{"compatible": "%(c)s", "version": "1.1.0"}' ;;
    install)
        echo "rauc $*" >> %(b)s/rauc.log
        echo "expect $(cat %(b)s/run/boat-update/expect 2>/dev/null)" >> %(b)s/rauc.log
        echo "  0%% Installing"
        if [ -f "$2" ]; then
            if [ -s %(b)s/rauc-error ]; then echo "LastError: $(cat %(b)s/rauc-error)"; exit 1; fi
            sed "s/B_VERSION/$(cat %(b)s/rauc-serves)/" %(b)s/rauc-status.in > %(b)s/rauc-status
            grub-editenv %(e)s set "ORDER=B A" B_OK=1 B_TRY=0 B_COMMITTED=0
            echo "100%% Installing done."
            exit 0
        fi
        if [ -s %(b)s/rauc-error ]; then
            echo "LastError: $(cat %(b)s/rauc-error)"; echo "Installing failed" >&2; exit 1
        fi
        rc=$(cat %(b)s/rauc-exit)
        if [ "$rc" != 0 ]; then
            echo "RAUC service disappeared" >&2; exit "$rc"
        fi
        # RAUC's NBD client: the "sqsh" magic first, then blocks further in.
        a=$(curl --noproxy '*' -s -o /dev/null -w '%%{http_code}' -r 0-3 "$2")
        b=$(curl --noproxy '*' -s -o /dev/null -w '%%{http_code}' -r 1048576-1179647 "$2")
        echo "fetched $a $b" >> %(b)s/rauc.log
        if [ "$a" != 206 ] || [ "$b" != 206 ]; then
            echo "LastError: unexpected HTTP response code $a/$b"; exit 1
        fi
        sed "s/B_VERSION/$(cat %(b)s/rauc-serves)/" %(b)s/rauc-status.in > %(b)s/rauc-status
        grub-editenv %(e)s set "ORDER=B A" B_OK=1 B_TRY=0 B_COMMITTED=0
        echo "100%% Installing done." ;;
esac
""" % dict(b=board, c=COMPATIBLE, e=board + "/boot/efi/EFI/BOOT/grubenv"), 0o755)
        write(board + "/rauc-status.in", rauc_status("B_VERSION"))
        os.environ["PATH"] = binp + ":" + os.environ["PATH"]
        bu.PATHS.update({
            "cmdline": board + "/proc/cmdline", "mounts": board + "/proc/mounts",
            "grubenv": env, "release": board + "/etc/boat-release",
            "nv_release": board + "/etc/nv_tegra_release",
            "system_conf": board + "/etc/rauc/system.conf", "ca": board + "/etc/rauc/ca.cert.pem",
            "conf_image": board + "/etc/boat-update/channel.conf", "data": board + "/data",
            "state": board + "/data/boat-update", "run_ab": board + "/run/boat-ab",
            "lock": board + "/run/boat-update.lock", "expect": board + "/run/boat-update/expect",
            "clock_retry": board + "/run/boat-update/clock-retry"})
        bu.DISK = DISK
        bu.PART.update({"A": "1", "B": "2", "DATA": "18"})

        def run_check():
            state = bu.load_state()
            try:
                return bu.do_check(bu.load_conf(), state, quiet=True), None
            except bu.UpdateError as e:
                return None, str(e)

        def status():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bu.main(["boat-update", "status"])
            return out.getvalue()

        # --- the channel's TLS CA --------------------------------------------
        publish(7)
        offer, err = run_check()
        check("a channel whose TLS CA the board does not trust is refused",
              offer is None and err and "curl exit 60" in err, err)
        shutil.copy(keys + "/tlsca.pem", board + "/data/boat-update/server-ca.pem")
        offer, err = run_check()
        check("... and /data/boat-update/server-ca.pem makes the board trust it", offer is not None, err)
        os.unlink(board + "/data/boat-update/state.json")

        # --- refusals before any download ----------------------------------
        publish(7, signer="otherindex")
        offer, err = run_check()
        check("an index signed under another CA is refused", offer is None and err and "does not verify" in err, err)
        publish(7, signer="bundlekey")
        offer, err = run_check()
        check("an index signed with the bundle (codeSigning) key is refused",
              offer is None and err and "does not verify" in err, err)
        publish(7, channel="testing")
        offer, err = run_check()
        check("a 'testing' index served at 'stable' is refused", offer is None and err and "channel" in err, err)

        # --- accept, then replay -----------------------------------------
        publish(7)
        offer, err = run_check()
        check("a good index is accepted and its bundle offered", offer is not None and offer["version"] == "1.1.0", err)
        state = bu.load_state()
        check("control characters are stripped from the notes", state.get("notes") == "test release", repr(state.get("notes")))
        offer, err = run_check()
        check("the same index again (a re-check) is still accepted", offer is not None, err)
        publish(6)
        offer, err = run_check()
        check("an older serial (a replay) is refused", offer is None and err and "replayed" in err, err)
        publish(7, notes="different")
        offer, err = run_check()
        check("a different index with the same serial is refused", offer is None and err and "replayed" in err, err)
        out = status()
        check("status shows the refused check as an alert",
              re.search(r"ALERT +the last check failed, .*replayed", out) is not None, out)

        # --- download: metered, interrupted, resumed, verified -------------
        publish(8)
        offer, err = run_check()
        check("... until a check works again", "ALERT" not in status() and "last_error" not in bu.load_state(),
              status())

        # --- offline: the board's state, not an alert ----------------------
        write(binp + "/ip", "#!/bin/sh\nexit 0\n", 0o755)
        try:
            bu.do_check(bu.load_conf(), bu.load_state(), quiet=True)
            err = None
        except bu.NotNow as e:
            err = str(e)
        write(binp + "/ip", "#!/bin/sh\necho 'default via 127.0.0.1 dev lo'\n", 0o755)
        check("with no default route a check is refused as the board's state, not an alert",
              err and "no network" in err and "last_error" not in bu.load_state(), str(err))
        try:
            bu.do_check(dict(bu.load_conf(), BOAT_UPDATE_URL="https://127.0.0.1:1/boat-xavier-nx"),
                        bu.load_state(), quiet=True)
            err = None
        except bu.NotNow as e:
            err = str(e)
        check("... and so is a channel nothing answers for",
              err and "cannot reach the channel" in err and "last_error" not in bu.load_state()
              and "ALERT" not in status(), str(err))
        conf = bu.load_conf()
        write(board + "/metered", "yes (guessed)\n")
        state = bu.load_state()
        r = bu.do_download(conf, state, windows=1)
        check("on a metered link it waits instead of downloading",
              r is False and "metered" in bu.load_state().get("waiting", ""), str(bu.load_state()))
        write(board + "/metered", "no (guessed)\n")
        Channel.cut_after = 1024 * 1024
        state = bu.load_state()
        r1 = bu.do_download(conf, state, windows=1)
        part = board + "/data/boat-update/downloads/%s.part" % bundle_name
        have = os.path.getsize(part) if os.path.exists(part) else -1
        check("a dropped link leaves a partial download behind", r1 is False and 0 < have < len(bundle_bytes),
              "ready=%s part=%d" % (r1, have))
        state = bu.load_state()
        r2 = bu.do_download(conf, state, windows=2)
        final = board + "/data/boat-update/downloads/" + bundle_name
        check("the next window resumes it to a verified, ready bundle",
              r2 is True and os.path.exists(final) and bu.sha256_file(final) == sha
              and bu.load_state().get("ready", {}).get("version") == "1.1.0",
              "ready=%s state=%s" % (r2, bu.load_state()))

        # --- a corrupted download is fetched again --------------------------
        os.unlink(final)
        st = bu.load_state()
        st.pop("ready", None)
        bu.save_state(st)
        with open(part, "wb") as f:
            f.write(b"\0" * len(bundle_bytes))
        r3 = bu.do_download(conf, bu.load_state(), windows=2)
        check("a download that fails its SHA-256 is fetched again",
              r3 is True and bu.load_state().get("refetch", {}).get(sha) == 1,
              "ready=%s state=%s" % (r3, bu.load_state()))

        # --- a hostile or broken channel host ------------------------------
        def reset_download():
            for p in (final, part):
                if os.path.exists(p):
                    os.unlink(p)
            st = bu.load_state()
            st.pop("ready", None)
            bu.save_state(st)

        def refetches():
            return bu.load_state().get("refetch", {}).get(sha, 0)

        Channel.mode = "chunked-endless"
        offer, err = run_check()
        check("an endless chunked index is cut off at %d bytes" % bu.INDEX_MAX,
              offer is None and err and "larger than" in err, str(err))
        Channel.mode = None
        offer, err = run_check()
        reset_download()
        before = refetches()
        Channel.mode = "chunked-oversize"
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            r = bu.do_download(conf, bu.load_state(), windows=1)
        # Cut off as it arrives - not written out in full and caught by the
        # SHA-256 afterwards, which would fill /data first.
        check("a bundle body past the index's size is cut off as it arrives, thrown away and counted",
              r is False and not os.path.exists(part) and refetches() == before + 1
              and "sent more than" in said.getvalue() and "1.1.0" not in bu.failed_versions(),
              "%s / %s" % (said.getvalue().strip(), bu.load_state()))
        reset_download()
        st = bu.load_state()
        st.pop("refetch", None)
        bu.save_state(st)
        with open(part, "wb") as f:
            f.write(bundle_bytes[:1024 * 1024])
        Channel.mode = "ignore-range"
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("a server that ignores Range starts the download over - counted, so it cannot loop for ever",
              r is False and not os.path.exists(part) and refetches() == 1
              and "1.1.0" not in bu.failed_versions(), str(bu.load_state()))
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("... and the fresh download completes", r is True and os.path.exists(final), str(bu.load_state()))
        # A bundle that takes more than one window: every resume is answered
        # with the whole file. One window for the whole file then finishes it.
        reset_download()
        st = bu.load_state()
        st.pop("refetch", None)
        bu.save_state(st)
        window, bu.WINDOW_SECONDS = bu.WINDOW_SECONDS, 1
        slow = dict(conf, BOAT_UPDATE_RATE_LIMIT="1m")
        r = bu.do_download(slow, bu.load_state())
        bu.WINDOW_SECONDS = window
        check("... a bundle longer than one window from such a server is fetched whole, not in an endless loop",
              r is True and os.path.exists(final) and refetches() == 1, str(bu.load_state()))
        reset_download()
        with open(part, "wb") as f:
            f.write(bundle_bytes[:1024 * 1024])
        Channel.mode = "refuse-range"
        before = refetches()
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("a server that refuses to resume (HTTP 416) starts over, counted",
              r is False and not os.path.exists(part) and refetches() == before + 1, str(bu.load_state()))
        Channel.mode = None
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("... and the fresh download completes", r is True and os.path.exists(final), str(bu.load_state()))
        # Complete and renamed, but the run stopped before it was recorded.
        st = bu.load_state()
        st.pop("ready", None)
        bu.save_state(st)
        with open(board + "/channel-hits", "w"):
            pass
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("a finished download the run never recorded is picked up, not fetched again",
              r is True and bu.load_state().get("ready", {}).get("sha256") == sha, str(bu.load_state()))
        reset_download()
        disk_usage = bu.shutil.disk_usage
        bu.shutil.disk_usage = lambda p: disk_usage(p)._replace(free=1 << 20)
        try:
            bu.do_download(conf, bu.load_state(), windows=1)
            check("a download /data has no room for is refused", False, "it went ahead")
        except bu.UpdateError as e:
            check("a download /data has no room for is refused", "MiB free" in str(e), str(e))
        finally:
            bu.shutil.disk_usage = disk_usage
        out = status()
        check("status shows the refused download as an alert",
              re.search(r"ALERT +the last download failed, .*MiB free", out) is not None
              and "/data is not mounted" not in out, out)
        mounts = open(board + "/proc/mounts").read()
        write(board + "/proc/mounts", mounts.replace(board + "/data", board + "/elsewhere"))
        try:
            bu.do_download(conf, bu.load_state(), windows=1)
            err = None
        except bu.UpdateError as e:
            err = str(e)
        out = status()
        write(board + "/proc/mounts", mounts)
        check("without /data mounted a download is refused, and status says why",
              err and "/data is not mounted" in err
              and re.search(r"ALERT +/data is not mounted from DATA", out) is not None
              and "MiB free" in bu.load_state()["last_error"]["message"], "%s\n%s" % (err, out))
        offer, err = run_check()
        check("... a check that still offers the bundle does not clear it",
              offer is not None and "the last download failed" in status(), "%s %s" % (err, status()))
        saved_offer = dict(bu.load_state()["offer"])
        write(board + "/rauc-info-fails", "")
        try:
            bu.do_download(conf, bu.load_state(), windows=1)
            check("a bundle RAUC rejects is given up, not offered for install", False, "it was accepted")
        except bu.UpdateError as e:
            st = bu.load_state()
            check("a bundle RAUC rejects is given up, not offered for install",
                  "RAUC rejects it" in str(e) and sha in st.get("given_up", {}) and "ready" not in st
                  and not os.path.exists(final), "%s / %s" % (e, st))
        os.unlink(board + "/rauc-info-fails")
        st = bu.load_state()
        st.pop("given_up", None)
        st["offer"] = saved_offer
        bu.save_state(st)
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("... a download that finishes does", r is True and "last_error" not in bu.load_state(),
              str(bu.load_state()))

        # --- housekeeping once the update is running and committed ----------
        write(board + "/etc/boat-release", "1.1.0\n")
        bu.housekeeping(bu.load_state())
        check("a committed update's bundle file is deleted",
              not os.path.exists(final) and "ready" not in bu.load_state(), str(bu.load_state()))
        offer, err = run_check()
        check("once running, the same version is no longer offered", offer is None and err is None,
              "offer=%s err=%s" % (offer, err))
        write(board + "/etc/boat-release", "1.0.0\n")
        bu.mark_failed("1.1.0")
        publish(9)
        offer, err = run_check()
        check("a version that failed a trial here is not offered again",
              offer is None and "failed a trial" in bu.load_state().get("message", ""), str(bu.load_state()))

        # --- transfer faults give a bundle up, they do not fail the version -
        publish(10, version="1.2.0")
        offer, err = run_check()
        st = bu.load_state()
        try:
            for _ in range(bu.MAX_REFETCH + 1):
                bu.refetch(st, st["offer"], "a test fault")
        except bu.UpdateError:
            pass
        st = bu.load_state()
        check("after %d re-fetches a bundle is given up - and not recorded as failing a trial"
              % bu.MAX_REFETCH, sha in st.get("given_up", {}) and "1.2.0" not in bu.failed_versions()
              and "offer" not in st, str(st))
        offer, err = run_check()
        check("a given-up bundle is not fetched again under the same index",
              offer is None and "given up" in bu.load_state().get("message", ""), str(bu.load_state()))
        publish(11, version="1.2.0")
        offer, err = run_check()
        check("a new index offers it again", offer is not None, err)

        # --- only an index-signing certificate may sign the index ----------
        publish(12, signer="noeku")
        offer, err = run_check()
        check("an index signed by a certificate with no extended key usage is refused",
              offer is None and err and "emailProtection" in err, err)
        idx = dict(base, published="2026\x1b[2J")
        try:
            bu.validate_index(idx)
            check("a control character in a field shown to the operator is refused", False, "accepted")
        except bu.UpdateError:
            check("a control character in a field shown to the operator is refused", True)

        # --- serials are per channel ---------------------------------------
        st = bu.load_state()
        conf_t = dict(bu.load_conf(), BOAT_UPDATE_CHANNEL="testing")
        idx = dict(base, channel="testing", serial=3, version="1.3.0", sha256=sha)
        offer, msg = bu.decide(idx, conf_t, st, "1.0.0", "1.0.0", COMPATIBLE, "35.6.4", set())
        check("switching to a channel that last published earlier is not a replay", offer is not None, msg)
        # stable -> testing -> stable, with stable's index unchanged throughout
        publish(11, version="1.2.0")
        offer, err = run_check()
        publish(3, channel="testing", version="1.3.0", where="testing")
        write(board + "/data/boat-update/channel.conf", "BOAT_UPDATE_CHANNEL=testing\n")
        offer_t, err_t = run_check()
        os.unlink(board + "/data/boat-update/channel.conf")
        offer, err = run_check()
        check("coming back to a channel whose index has not changed is not a replay",
              err_t is None and offer_t is not None and err is None, "%s / %s" % (err_t, err))

        # --- a downloaded bundle no longer worth installing ----------------
        st = bu.load_state()
        stale = os.path.join(bu.downloads_dir(), "boat-bundle-1.1.5-f.raucb")
        write(stale, "x")
        st["ready"] = {"bundle": os.path.basename(stale), "version": "1.1.5", "sha256": "f" * 64}
        st["offer"] = {"bundle": os.path.basename(stale), "version": "1.1.5", "sha256": "f" * 64,
                       "size": 1, "l4t": "35.6.4"}
        bu.save_state(st)
        write(board + "/etc/boat-release", "1.2.0\n")
        check("a downloaded bundle older than the running release is never installed",
              bu.ready_to_install(bu.load_state()) is None and not os.path.exists(stale),
              str(bu.load_state()))
        write(board + "/etc/boat-release", "1.0.0\n")

        # --- a trial that never reached boat-commit ------------------------
        subprocess.run(["grub-editenv", env, "set", "B_COMMITTED=0", "B_TRY=1"], check=True)
        st = bu.load_state()
        st["installed"] = {"version": "1.4.0", "slot": "B"}
        bu.save_state(st)
        bu.housekeeping(bu.load_state())
        check("a trial that ended before boat-commit (a panic, a power cut) is recorded as failed",
              "1.4.0" in bu.failed_versions() and "installed" not in bu.load_state(), str(bu.load_state()))
        said = open(board + "/data/boat-update/last-result").read()
        check("... with a reason, in last-result and history",
              "trial of 1.4.0 in slot B ended without a verdict" in said
              and said in open(board + "/data/boat-update/history").read(), said)

        # --- streaming (Phase 6): what RAUC is handed, and its failure paths -
        write(board + "/etc/boat-release", "1.0.0\n")
        subprocess.run(["grub-editenv", env, "set", "ORDER=A B", "B_OK=1", "B_TRY=0", "B_COMMITTED=1"],
                       check=True)
        publish(13, version="1.2.0")
        with open(board + "/data/boat-update/failed-versions", "w"):
            pass
        offer, err = run_check()
        # A real client certificate: the preflight's curl loads it.
        make_leaf(keys, "tlsca", "client", "clientAuth", cn="boat")
        key_pem = open(keys + "/client.key").read()
        write(board + "/data/boat-update/client.key", key_pem, 0o600)
        write(board + "/data/boat-update/client.crt", open(keys + "/client.pem").read(), 0o600)
        key_line = key_pem.splitlines()[1]
        write(board + "/rauc-serves", "1.2.0")
        conf = bu.load_conf()
        os.environ["BOAT_UPDATE_INNER"] = "1"

        def stream():
            try:
                bu.do_install(conf, bu.load_state(), stream=True)
                return None
            except bu.UpdateError as e:
                return str(e)

        def envvar(name):
            return subprocess.run(["grub-editenv", env, "list"], capture_output=True, text=True,
                                  check=True).stdout.count("%s\n" % name) == 1

        err = stream()
        log = open(board + "/rauc.log").read()
        want_url = "%s/stable/%s" % (url, bundle_name)
        check("a streamed install hands RAUC only the local relay's URL, with this stream's token - "
              "no certificate, no key",
              err is None and re.search(r"rauc install http://127\.0\.0\.1:\d+/[A-Za-z0-9_-]{24}"
                                        r"/boat-xavier-nx/stable/%s\n" % re.escape(bundle_name), log)
              and "--tls" not in log and key_line not in log, "%s\n%s" % (err, log))
        check("... and RAUC's Range requests are answered through it, from the channel",
              "fetched 206 206" in log, log)
        check("the client key stays root-only (RAUC never opens it)",
              oct(os.stat(board + "/data/boat-update/client.key").st_mode & 0o777) == "0o600")
        check("a streamed install is remembered for its trial",
              bu.load_state().get("installed") == {"version": "1.2.0", "slot": "B"}, str(bu.load_state()))
        write(board + "/rauc-error", "HTTP range request not supported")
        err = stream()
        check("a failed stream says so, and points at download-then-install",
              err is not None and "range request" in err and "boat-update download" in err, str(err))
        check("... and the release it replaced is no longer waiting for a trial",
              "installed" not in bu.load_state() and "1.2.0" not in bu.failed_versions(), str(bu.load_state()))
        write(board + "/rauc-error", "")
        write(board + "/rauc-exit", "2")
        err = stream()
        check("RAUC going away mid-install is a failure, not an install",
              err is not None and "rauc exit 2" in err and "installed" not in bu.load_state(), str(err))
        write(board + "/rauc-exit", "0")
        write(board + "/rauc-serves", "1.1.0")
        err = stream()
        check("a channel that streams another (signed) release than the index offered: refused, "
              "and that slot never boots",
              err is not None and "index offered 1.2.0" in err and envvar("B_OK=0") and envvar("ORDER=A B")
              and "installed" not in bu.load_state(), str(err))
        write(board + "/rauc-serves", "1.2.0")
        with open(board + "/rauc.log", "w"):
            pass
        bu.mark_failed("1.2.0")
        err = stream()
        check("a version that failed a trial here is not streamed",
              err is not None and "nothing newer" in err and not open(board + "/rauc.log").read(), str(err))
        with open(board + "/data/boat-update/failed-versions", "w"):
            pass
        offer, err = run_check()
        write(board + "/etc/boat-release", "1.2.0\n")
        err = stream()
        check("an offer no newer than the running release is not streamed (it would overwrite the rollback)",
              err is not None and "nothing newer" in err and not open(board + "/rauc.log").read()
              and "offer" not in bu.load_state(), str(err))
        write(board + "/etc/boat-release", "1.0.0\n")
        offer, err = run_check()
        for mode, what, why in (("redirect", "redirects the bundle", "redirects"),
                                ("ignore-range", "ignores Range requests", "Range")):
            Channel.mode = mode
            err = stream()
            Channel.mode = None
            check("a channel that %s is refused before RAUC starts" % what,
                  err is not None and why in err and not open(board + "/rauc.log").read(), str(err))
        write(board + "/data/boat-update/channel.conf", "BOAT_UPDATE_CHANNEL=testing\n")
        conf = bu.load_conf()
        err = stream()
        os.unlink(board + "/data/boat-update/channel.conf")
        conf = bu.load_conf()
        check("an offer checked on another channel is not streamed from this one",
              err is not None and "channel has changed" in err and not open(board + "/rauc.log").read(), str(err))
        del os.environ["BOAT_UPDATE_INNER"]

        # --- the relay itself: what it will not pass on to RAUC -------------
        def through_relay(dev="lo"):
            relay, local_url = bu.start_relay(conf, want_url, dev)
            try:
                return subprocess.run(["curl", "--noproxy", "*", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                       "-r", "0-3", local_url], capture_output=True, text=True).stdout
            finally:
                relay.shutdown()
                relay.server_close()
        check("the relay answers a Range request from the channel", through_relay() == "206")
        for mode, what in (("redirect", "a redirect the channel sends later"),
                           ("ignore-range", "a full body instead of the range"),
                           ("shifted-range", "a range other than the one asked for"),
                           ("short-range", "a range shorter than the one asked for")):
            Channel.mode = mode
            got = through_relay()
            Channel.mode = None
            check("the relay never passes on %s" % what, got == "502", got)
        check("the relay never leaves the link that was checked",
              through_relay(dev="boatnodev0") == "502")

        def relay_get(rng, path=None):
            relay, local_url = bu.start_relay(conf, want_url, "lo")
            try:
                u = local_url.split("/", 3)
                host, port = u[2].split(":")
                c = http.client.HTTPConnection(host, int(port), timeout=10)
                c.request("GET", path or "/" + u[3], headers={"Range": rng})
                return c.getresponse().status
            finally:
                relay.shutdown()
                relay.server_close()
        bundle_path = urllib.parse.urlsplit(want_url).path
        check("the relay serves nothing without this stream's token - the bundle's own path is not enough",
              relay_get("bytes=0-3", path=bundle_path) == 403)
        write(board + "/rauc-cgroup", "/system.slice/rauc.service\n")
        got = relay_get("bytes=0-3")
        write(board + "/rauc-cgroup", own_cgroup + "\n")
        check("... nor a process outside rauc.service, even with RAUC's uid and the token "
              "(root in a container sharing the host's PID and network namespaces)",
              got == 403 and relay_get("bytes=0-3") == 206, str(got))
        check("the relay refuses a range that ends before it starts (it would read the whole bundle)",
              relay_get("bytes=1000-0") == 400)
        uids, bu.relay_uids = bu.relay_uids, lambda: {0x7ffffff0}
        got = relay_get("bytes=0-3")
        bu.relay_uids = uids
        check("the relay serves no one but RAUC's streaming client (and root)", got == 403, str(got))

        # --- more of the board's state ----------------------------------------
        def setenv(**kw):
            subprocess.run(["grub-editenv", env, "set"] + ["%s=%s" % kv for kv in kw.items()], check=True)

        def genv():
            out = subprocess.run(["grub-editenv", env, "list"], capture_output=True, text=True, check=True).stdout
            return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)

        @contextlib.contextmanager
        def as_root():
            euid, os.geteuid = os.geteuid, lambda: 0
            try:
                yield
            finally:
                os.geteuid = euid

        mark = board + "/run/boat-ab/committed"

        def gate(cmdline, committed="1", committed_mark=False):
            write(board + "/proc/cmdline", cmdline + "\n")
            setenv(A_COMMITTED=committed)
            if committed_mark:
                write(mark, "")
            elif os.path.exists(mark):
                os.unlink(mark)
            try:
                bu.gates()
                return None
            except bu.UpdateError as e:
                return str(e)
        setenv(ORDER="A B", A_OK="1", A_TRY="0", B_OK="1", B_TRY="0", B_COMMITTED="1")
        err = gate("root=x rauc.slot=A boat.trial=1")
        check("on a trial boot, nothing is checked or installed until boat-commit commits it",
              err is not None and "on trial" in err, str(err))
        err = gate("root=x rauc.slot=A boat.trial=1", committed_mark=True)
        check("... and from the commit on, the same boot updates again (no reboot needed)", err is None, str(err))
        err = gate("root=x rauc.slot=A boat.lastresort=1", committed="0")
        check("GRUB's last-resort boot of a slot that never passed a trial can install - it is how it is mended",
              err is None, str(err))
        err = gate("root=x rauc.slot=A", committed="0")
        check("a slot that is not committed is on trial", err is not None and "on trial" in err, str(err))
        err = gate("root=/dev/nvme0n1p1")
        check("a boot from outside GRUB updates nothing", err is not None and "GRUB" in err, str(err))
        gate("root=x rauc.slot=A")

        # --- the other slot's release counts only if a rollback would boot it -
        publish(14, version="1.2.0")
        write(board + "/rauc-status", rauc_status("1.2.0", "failed"))
        setenv(B_OK="0", B_TRY="0")
        offer, err = run_check()
        check("a release whose install into the other slot failed half way is offered again",
              offer is not None and offer["version"] == "1.2.0", "%s %s" % (err, bu.load_state().get("message")))
        write(board + "/rauc-status", rauc_status("1.2.0"))
        offer, err = run_check()
        check("... and so is one in a slot GRUB no longer boots (used up, or a reflash's left-over slot B)",
              offer is not None, "%s %s" % (err, bu.load_state().get("message")))
        setenv(B_OK="1")
        offer, err = run_check()
        check("... but not one a rollback would boot",
              offer is None and "already in the other slot" in bu.load_state().get("message", ""),
              "%s %s" % (err, bu.load_state().get("message")))
        write(board + "/rauc-status", rauc_status("1.0.0"))

        # --- an offer the channel withdraws takes its download with it -------
        publish(15, version="1.2.0")
        offer, err = run_check()
        os.makedirs(bu.downloads_dir(), exist_ok=True)
        write(os.path.join(bu.downloads_dir(), bundle_name + ".part"), "half")
        publish(16, version="1.0.0")
        offer, err = run_check()
        check("an offer withdrawn by the next index leaves no download behind",
              offer is None and not os.listdir(bu.downloads_dir()) and "ready" not in bu.load_state(),
              "%s %s" % (os.listdir(bu.downloads_dir()), bu.load_state()))

        # --- the client key ----------------------------------------------------
        publish(17, version="1.2.0")
        os.chmod(board + "/data/boat-update/client.key", 0o644)
        offer, err = run_check()
        os.chmod(board + "/data/boat-update/client.key", 0o600)
        check("a client key others can read is refused, with the fix",
              offer is None and err and "chmod 600" in err, str(err))
        offer, err = run_check()

        # --- streaming over a VPN ----------------------------------------------
        write(board + "/nmtype", "wireguard\n")
        os.environ["BOAT_UPDATE_INNER"] = "1"
        with open(board + "/rauc.log", "w"):
            pass
        err = stream()
        write(board + "/nmtype", "loopback\n")
        check("a stream over a VPN default route is refused (the tunnel can move to a metered link)",
              err is not None and "VPN" in err and not open(board + "/rauc.log").read(), str(err))

        # --- a bundle file: the inner install, and what RAUC's handler said --
        incoming = board + "/data/boat-update/incoming/x.raucb"
        write(incoming, "bundle")
        write(board + "/rauc-serves", "1.1.0")

        def install_file():
            try:
                bu.do_install(conf, bu.load_state(), bundle=incoming)
                return None
            except bu.UpdateError as e:
                return str(e)
        err = install_file()
        log = open(board + "/rauc.log").read()
        check("a bundle file is installed through RAUC, told which release it must be",
              err is None and ("rauc install %s\n" % incoming) in log
              and ("expect 1.1.0 %d\n" % os.getpid()) in log and not os.path.exists(bu.path("expect"))
              and bu.load_state().get("installed") == {"version": "1.1.0", "slot": "B"},
              "%s\n%s\n%s" % (err, log, bu.load_state()))
        write(board + "/rauc-error", "Pre-install handler error: Child process exited with code 1")
        write(board + "/rauc-journal", "installing x: started\n# boat-rauc-pre-install: the bundle is for "
              "L4T 36.4.0, this board runs 35.6.4: a BSP change also changes firmware\n")
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            err = install_file()
        write(board + "/rauc-error", "")
        check("a refusal by RAUC's install handler is shown, not just its exit code",
              err is not None and "did not install" in err
              and "RAUC's install handler: boat-rauc-pre-install: the bundle is for L4T 36.4.0" in said.getvalue(),
              "%s / %s" % (err, said.getvalue()))
        del os.environ["BOAT_UPDATE_INNER"]

        # --- the outer wrapper: its own unit, and its exit status --------------
        setenv(ORDER="A B", B_OK="1", B_TRY="0", B_COMMITTED="1")
        with open(board + "/systemd.log", "w"):
            pass
        err = install_file()
        check("install runs in its own unit, and succeeds when the unit did",
              err is None and "systemd-run --unit=boat-update-install --setenv=BOAT_UPDATE_INNER=1"
              in open(board + "/systemd.log").read(), "%s\n%s" % (err, open(board + "/systemd.log").read()))
        write(board + "/unit-result", "exit-code\n")
        err = install_file()
        write(board + "/unit-result", "success\n")
        check("... and fails when the unit failed (push-update and ab-soak go by this)",
              err is not None and "the install failed (exit-code)" in err, str(err))

        # --- auto: the rate limit, and the clock -------------------------------
        with open(board + "/systemd.log", "w"):
            pass
        st = bu.load_state()
        st.pop("last_auto", None)
        st.pop("offer", None)
        bu.save_state(st)
        write(board + "/leap", "Not synchronised\n")
        try:
            bu.do_auto(conf, bu.load_state())
            check("auto before the clock is set is refused", False, "it ran")
        except bu.ClockNotSynced:
            check("auto before the clock is set does not use up the rate limit, and comes back once it is",
                  "last_auto" not in bu.load_state()
                  and "--unit=boat-update-check-retry" in open(board + "/systemd.log").read()
                  and "RemainAfterElapse=no" in open(board + "/systemd.log").read()
                  and "last_error" not in bu.load_state(),
                  "%s\n%s" % (bu.load_state(), open(board + "/systemd.log").read()))
        for _ in range(7):
            with contextlib.suppress(bu.ClockNotSynced):
                bu.do_auto(conf, bu.load_state())
        waits = re.findall(r"--on-active=(\d+)", open(board + "/systemd.log").read())
        check("... sooner at first, then less and less often, then only on the timer's schedule",
              waits == [str(bu.CLOCK_RETRY << n) for n in range(bu.CLOCK_RETRIES)], str(waits))
        with as_root():
            rc = bu.main(["boat-update", "auto"])
        check("auto refused for the board's state exits 0 - the timer's unit has not failed, and SIGTERM unwinds",
              rc == 0 and signal.getsignal(signal.SIGTERM) is bu.terminated, str(rc))
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        write(board + "/leap", "Normal\n")
        publish(18, version="1.0.0")
        bu.do_auto(conf, bu.load_state())
        st = bu.load_state()
        check("... and then it checks, and the retries start over",
              st.get("serials", {}).get("stable") == 18 and "last_auto" in st
              and not os.path.exists(bu.PATHS["clock_retry"]), str(st))
        st["last_check"] = 1
        bu.save_state(st)
        bu.do_auto(conf, bu.load_state())
        check("a second trigger within the rate limit, with nothing on offer, does nothing",
              bu.load_state().get("last_check") == 1)
        st = bu.load_state()
        st["last_auto"] = int(time.time()) + 3600
        bu.save_state(st)
        bu.do_auto(conf, bu.load_state())
        check("a rate-limit stamp from the future (a clock that was ahead) does not count",
              bu.load_state().get("last_check") != 1)

        # --- download's exit status --------------------------------------------
        with as_root():
            rc = bu.main(["boat-update", "download"])
        check("download exits 2 when no bundle became ready", rc == 2, str(rc))

        # --- rollback, without RAUC ----------------------------------------------
        with open(board + "/rauc.log", "w"):
            pass
        with open(board + "/systemd.log", "w"):
            pass
        setenv(ORDER="A B", B_OK="1", B_TRY="1", B_COMMITTED="1")
        with as_root():
            try:
                bu.do_rollback(yes=True)
                err = None
            except bu.UpdateError as e:
                err = str(e)
        e = genv()
        check("rollback switches GRUB to the other slot itself - RAUC's service needs /data, which may be what broke",
              err is None and e.get("ORDER") == "B A" and e.get("B_OK") == "1" and e.get("B_TRY") == "0"
              and "mark-active" not in open(board + "/rauc.log").read()
              and "systemctl reboot" in open(board + "/systemd.log").read(), "%s %s" % (err, e))
        setenv(ORDER="A B", B_OK="0")
        with as_root():
            try:
                bu.do_rollback(yes=True)
                err = None
            except bu.UpdateError as e:
                err = str(e)
        check("... and refuses when there is no usable slot to go to",
              err is not None and "not usable" in err and genv().get("ORDER") == "A B", str(err))
        setenv(B_OK="1")

        # --- a running release that is not a version ---------------------------
        try:
            bu.decide(dict(base, serial=99), bu.load_conf(), {}, "v1.3.0", None, COMPATIBLE, "35.6.4", set())
            check("a board whose release is not a version takes no offer", False, "it took one")
        except bu.UpdateError as e:
            check("a board whose release is not a version takes no offer", "not a version" in str(e), str(e))

        # --- a person's command and a background download ------------------------
        with open(board + "/systemd.log", "w"):
            pass
        wait, bu.LOCK_WAIT = bu.LOCK_WAIT, 2
        held = open(bu.PATHS["lock"], "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            with bu.locked(interactive=True):
                pass
            err = None
        except bu.UpdateError as e:
            err = str(e)
        check("a person's command does not stop a background run that is not there (an install holds the lock)",
              err is not None and "stop boat-update-check.service" not in open(board + "/systemd.log").read(),
              str(err))
        # A Type=oneshot unit is "activating" for as long as it runs.
        write(board + "/unit-state", "activating\n")
        try:
            with bu.locked(interactive=True):
                pass
            err = None
        except bu.UpdateError as e:
            err = str(e)
        check("a person's command stops a background check or download, and waits for it",
              err is not None and "stop boat-update-check.service" in open(board + "/systemd.log").read(), str(err))
        threading.Timer(1, lambda: fcntl.flock(held, fcntl.LOCK_UN)).start()
        try:
            with bu.locked(interactive=True):
                pass
            err = None
        except bu.UpdateError as e:
            err = str(e)
        check("... and goes ahead once it has stopped", err is None, str(err))
        held.close()
        bu.LOCK_WAIT = wait
        os.unlink(board + "/unit-state")
        httpd.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d check(s) failed" % len(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
