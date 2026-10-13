#!/usr/bin/env python3
"""Exercise boat-update against a real HTTPS update channel on localhost.

    scripts/test-boat-update.py       # needs openssl, curl, grub-editenv

boat-update decides, from a file anyone on the internet could serve, what
gets written into the boat's other slot. This runs it, as shipped, against:

  - a throwaway update CA, made here the same way scripts/gen-update-keys.sh
    makes the real one (an index-signing leaf with emailProtection, a bundle
    signer with codeSigning), plus a second, unrelated CA;
  - a local HTTPS server with Range support that can also cut a download
    off half way, the way a dropped link does;
  - a fake board (cmdline, mounts, grubenv, /data), with rauc, nmcli, ip,
    chronyc and blkid stubbed.

It checks that a good index is accepted and its bundle downloaded - across
an interrupted transfer - and verified; and that an index signed by
another CA, signed with the bundle key, replayed, or published for another
channel is refused before anything is downloaded. A misbehaving host gets
its own cases: an endless chunked index or bundle (cut off at the signed
size as it arrives - kirkstone's curl 7.82 would not), and a server that
ignores Range. So do give-ups, per-channel serials, a stale ready bundle and
a trial that died before boat-commit. It also checks the pure parts
(version order as RAUC defines it, index validation) directly.

Exit status: 0 = all passed, 1 = a case failed, 77 = a tool is missing.
"""
import contextlib
import hashlib
import io
import http.server
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading

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
    curl to check - "ignore-range" answers a Range request with 200, and
    "redirect" sends a bundle request somewhere else."""
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
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
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
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(keys + "/server.pem", keys + "/server.key")
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = "https://localhost:%d/boat-xavier-nx" % httpd.server_address[1]
        os.environ["CURL_CA_BUNDLE"] = keys + "/tlsca.pem"   # test only
        os.environ["SSL_CERT_FILE"] = keys + "/tlsca.pem"    # the stream relay's Python TLS

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
        write(binp + "/nmcli", "#!/bin/sh\nprintf '%%s\\nloopback\\n' \"$(cat %s/metered)\"\n" % board, 0o755)
        write(binp + "/chronyc", "#!/bin/sh\necho 'Leap status     : Normal'\n", 0o755)
        write(binp + "/blkid", "#!/bin/sh\nexit 2\n", 0o755)
        # RAUC: slot B holds 1.0.0; "info" accepts the test bundle. A
        # streamed "install" logs its arguments; then it fails with
        # rauc-error (LastError), or exits with rauc-exit as a RAUC that
        # went away does, or puts the release in rauc-serves into slot B
        # and activates it, as RAUC would.
        def rauc_status(b_version):
            return json.dumps({"slots": [
                {"rootfs.0": {"bootname": "A", "slot_status": {"bundle": {"version": "1.0.0"}}}},
                {"rootfs.1": {"bootname": "B", "slot_status": {"bundle": {"version": b_version}}}}]})
        write(board + "/rauc-status", rauc_status("1.0.0"))
        write(board + "/rauc-error", "")
        write(board + "/rauc-exit", "0")
        write(binp + "/rauc", """#!/bin/sh
case "$1" in
    status) cat %(b)s/rauc-status ;;
    info) echo '{"compatible": "%(c)s", "version": "1.1.0"}' ;;
    install)
        echo "rauc $*" >> %(b)s/rauc.log
        echo "  0%% Installing"
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
            "lock": board + "/run/boat-update.lock"})
        bu.DISK = DISK
        bu.PART.update({"A": "1", "B": "2", "DATA": "18"})

        def run_check():
            state = bu.load_state()
            try:
                return bu.do_check(bu.load_conf(), state, quiet=True), None
            except bu.UpdateError as e:
                return None, str(e)

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

        # --- download: metered, interrupted, resumed, verified -------------
        publish(8)
        offer, err = run_check()
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
        with open(part, "wb") as f:
            f.write(bundle_bytes[:1024 * 1024])
        before = refetches()
        Channel.mode = "ignore-range"
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("a server that ignores Range starts the download over - not counted against the bundle",
              r is False and not os.path.exists(part) and refetches() == before
              and "1.1.0" not in bu.failed_versions(), str(bu.load_state()))
        r = bu.do_download(conf, bu.load_state(), windows=1)
        check("... and the fresh download completes", r is True and os.path.exists(final), str(bu.load_state()))
        Channel.mode = None

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
        check("a streamed install hands RAUC only the local relay's URL - no certificate, no key",
              err is None and re.search(r"rauc install http://127\.0\.0\.1:\d+/boat-xavier-nx/stable/%s\n"
                                        % re.escape(bundle_name), log)
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
                           ("ignore-range", "a full body instead of the range")):
            Channel.mode = mode
            got = through_relay()
            Channel.mode = None
            check("the relay never passes on %s" % what, got == "502", got)
        check("the relay never leaves the link that was checked",
              through_relay(dev="boatnodev0") == "502")
        httpd.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d check(s) failed" % len(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
