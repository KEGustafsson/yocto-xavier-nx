#!/usr/bin/env python3
"""boat-update: OS updates for the boat computer's A/B layout.

    boat-update [status]          where things stand (read-only; the default)
    boat-update check             fetch and verify the channel's signed index
    boat-update download          fetch the offered bundle, resumably
    boat-update install [BUNDLE] [--reboot]
                                  install the ready bundle (or BUNDLE) into
                                  the other slot; it boots as a trial
    boat-update rollback [--yes]  switch to the other slot and reboot
    boat-update history           installs, trials and their results
    boat-update debug-hold on|off keep the board up after a failure
                                  (boat.stay=1) instead of resetting
    boat-update auto              what the timer runs: check, then download
                                  as BOAT_UPDATE_AUTO allows

Nothing here ever installs or reboots on its own: an update reaches the
other slot only through `install`, typed by a person (plan D12).

The update channel is static files on any HTTPS host with Range support:
index.p7 - a CMS-signed JSON index - and the content-addressed bundles it
names. The index is verified against the update CA in /etc/rauc, never
against the system's public CAs, and accepted only if it is for this
channel, newer than the last one accepted (serial), for this board
(compatible, L4T release) and offers a version newer than the running one,
not already in the other slot and not one that failed a trial here. RAUC
then verifies the bundle itself, again, before installing it.

Configuration: /etc/boat-update/channel.conf (the image's defaults) and
/data/boat-update/channel.conf (the operator's overrides, shared by both
slots). State lives in /data/boat-update. See docs/07-updates.md and
docs/plans/ab-os-updates.md sections 5.6 and 5.10.
"""
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

# Filled in at build time by the recipe: the A/B layout's disk and
# partition numbers (BOAT_AB_* in meta-boat's layer.conf).
DISK = "@DISK@"
PART = {"A": "@PARTNUM_A@", "B": "@PARTNUM_B@", "DATA": "@PARTNUM_DATA@"}
PART_NAME = {"A": "APP", "B": "APP_b"}

PATHS = {
    "cmdline": "/proc/cmdline",
    "mounts": "/proc/mounts",
    "grubenv": "/boot/efi/EFI/BOOT/grubenv",
    "release": "/etc/boat-release",
    "nv_release": "/etc/nv_tegra_release",
    "system_conf": "/etc/rauc/system.conf",
    "ca": "/etc/rauc/ca.cert.pem",
    "conf_image": "/etc/boat-update/channel.conf",
    "data": "/data",
    "state": "/data/boat-update",
    "run_ab": "/run/boat-ab",
    "lock": "/run/boat-update.lock",
}

DEFAULTS = {
    "BOAT_UPDATE_URL": "",
    "BOAT_UPDATE_CHANNEL": "stable",
    "BOAT_UPDATE_AUTO": "download",
    "BOAT_UPDATE_METERED": "no",
    "BOAT_UPDATE_RATE_LIMIT": "",
    "BOAT_UPDATE_CLIENT_CERT": "",
    "BOAT_UPDATE_CLIENT_KEY": "",
}

INDEX_MAX = 64 * 1024            # bytes; an index is a few hundred
NOTES_MAX = 2000                 # characters shown from an index's notes
WINDOW_SECONDS = 600             # one download window
STALL_BYTES, STALL_SECONDS = 1024, 120
FREE_FLOOR = 2 * 1024 ** 3       # /data headroom beyond the bundle (Docker)
MAX_REFETCH = 3                  # full re-downloads before a bundle is given up
SERIAL_MAX = 2 ** 32             # publish-update.sh uses at most the time in seconds
AUTO_MIN_INTERVAL = 30 * 60      # the timer and the triggers rate-limit to this
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.raucb$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """A refusal or failure with a message meant for the operator."""


def say(msg):
    print("boat-update: %s" % msg, flush=True)


def warn(msg):
    print("boat-update: WARNING: %s" % msg, file=sys.stderr, flush=True)


def path(name, *more):
    return os.path.join(PATHS[name], *more)


def read(p, default=""):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return default


def write_atomic(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".new"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


# --- versions: RAUC's relaxed semantic versioning ---------------------------
# MAJOR[.MINOR[.PATCH]][-PRERELEASE][+BUILD], compared the way RAUC 1.13's
# r_semver_less_equal() does (src/utils.c), so this tool and RAUC's
# min-bundle-version never disagree about which version is newer.

# No '-' inside the pre-release or build part: RAUC splits at the last '-'.
SEMVER_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?(?:\+[0-9A-Za-z.]+)?")


def parse_version(v):
    m = SEMVER_RE.fullmatch(v or "")
    if not m:
        raise ValueError("not a version: %r" % v)
    core = tuple(int(x) if x is not None else 0 for x in m.group(1, 2, 3))
    return core, m.group(4)


def version_key(v):
    """A sort key; a later version sorts higher."""
    core, pre = parse_version(v)
    if pre is None:
        return core + ((1,),)
    fields = []
    for f in pre.split("."):
        # Numeric identifiers sort below alphanumeric ones.
        fields.append((0, int(f), "") if f.isdigit() else (1, 0, f))
    return core + ((0, tuple(fields)),)


def newer(a, b):
    """True if version a is strictly newer than version b."""
    return version_key(a) > version_key(b)


# --- the board's state -----------------------------------------------------

def cmdline():
    args = {}
    for tok in read(path("cmdline")).split():
        k, _, v = tok.partition("=")
        args[k] = v
    return args


def booted_slot():
    s = cmdline().get("rauc.slot")
    return s if s in ("A", "B") else None


def other(slot):
    return {"A": "B", "B": "A"}.get(slot)


def part_dev(num):
    return "%sp%s" % (DISK, num)


def gpt_name(num):
    r = run(["blkid", "-p", "-o", "value", "-s", "PART_ENTRY_NAME", part_dev(num)])
    return r.stdout.strip() if r.returncode == 0 else ""


def mount_source(mountpoint):
    src = None
    for line in read(path("mounts")).splitlines():
        f = line.split()
        if len(f) >= 2 and f[1] == mountpoint:
            src = f[0]
    return src


def data_mounted():
    return mount_source(PATHS["data"]) == part_dev(PART["DATA"])


def grubenv():
    r = run(["grub-editenv", path("grubenv"), "list"])
    if r.returncode != 0:
        return None
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


@contextlib.contextmanager
def commit_lock():
    """boat-commit's lock: grubenv has one writer at a time."""
    os.makedirs(PATHS["run_ab"], exist_ok=True)
    with open(path("run_ab", "commit.lock"), "w") as f:
        for _ in range(60):
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(1)
        else:
            raise UpdateError("boat-commit is holding grubenv - try again in a minute")
        yield


def replace_durably(src, dst):
    """Rename src over dst so a power cut leaves one or the other. On vfat
    the moved file's directory entry is written only by an fsync of the
    file itself (see boat-ab's lib.sh)."""
    with open(src, "rb") as f:
        os.fsync(f.fileno())
    os.replace(src, dst)
    with open(dst, "rb") as f:
        os.fsync(f.fileno())
    d = os.open(os.path.dirname(dst), os.O_RDONLY)
    try:
        os.fsync(d)
    finally:
        os.close(d)


def grubenv_set(**kv):
    """Set variables in grubenv on a copy that replaces it, and keep
    grubenv.bak (GRUB's fallback) a copy of it - like boat-ab's lib.sh,
    under the same lock and never through its temporary file names."""
    env = path("grubenv")
    with commit_lock():
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(env), prefix=".boat-update-")
        os.close(fd)
        try:
            shutil.copyfile(env, tmp)
            r = run(["grub-editenv", tmp, "set"] + ["%s=%s" % i for i in kv.items()])
            if r.returncode != 0:
                raise UpdateError("grub-editenv failed: %s" % r.stderr.strip())
            replace_durably(tmp, env)
            shutil.copyfile(env, tmp)
            replace_durably(tmp, env + ".bak")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def running_release():
    return read(path("release")).strip() or "unknown"


def running_l4t():
    m = re.match(r"^# R(\d+) \(release\), REVISION: (\d+)\.(\d+)", read(path("nv_release")))
    return "%s.%s.%s" % m.groups() if m else None


def system_compatible():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(read(path("system_conf")))
        return cp.get("system", "compatible", fallback=None)
    except configparser.Error:
        return None


def rauc_slots():
    """bootname -> RAUC's detailed slot status (from --detailed JSON)."""
    r = run(["rauc", "status", "--detailed", "--output-format=json"])
    if r.returncode != 0:
        return None           # RAUC is busy installing, or not answering
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return None
    slots = {}
    for entry in doc.get("slots", []):
        for _, info in entry.items():
            if info.get("bootname"):
                slots[info["bootname"]] = info
    return slots


def slot_version(slot, slots, may_mount=False):
    """The release a slot holds: RAUC's record of the bundle installed
    there, or - for a slot that was flashed, not installed - the
    /etc/boat-release inside it, read through a read-only mount. Only a
    caller holding the update lock may mount (may_mount): RAUC may be
    writing that slot otherwise, and a mount there would fail its install."""
    if slots is None:
        return None
    info = slots.get(slot, {})
    v = ((info.get("slot_status") or {}).get("bundle") or {}).get("version")
    if v:
        return v
    if not may_mount or gpt_name(PART[slot]) != PART_NAME[slot]:
        return None
    with tempfile.TemporaryDirectory(prefix="boat-update-") as mnt:
        r = run(["mount", "-o", "ro,noload", "-t", "ext4", part_dev(PART[slot]), mnt])
        if r.returncode != 0:
            return None
        try:
            return read(os.path.join(mnt, "etc/boat-release")).strip() or None
        finally:
            run(["umount", mnt])


def failed_versions():
    return set(l.strip() for l in read(path("state", "failed-versions")).splitlines() if l.strip())


def mark_failed(version):
    os.makedirs(PATHS["state"], exist_ok=True)
    with open(path("state", "failed-versions"), "a") as f:
        f.write(version + "\n")


def load_state():
    try:
        state = json.loads(read(path("state", "state.json")) or "{}")
        if isinstance(state, dict):
            return state
    except ValueError:
        pass
    warn("state.json is unreadable - starting over: the next index accepted sets the serial again")
    return {}


def save_state(state):
    write_atomic(path("state", "state.json"), json.dumps(state, indent=1, sort_keys=True) + "\n")


def load_conf():
    """The two configuration layers: the image's, then the operator's."""
    conf = dict(DEFAULTS)
    for p in (path("conf_image"), path("state", "channel.conf")):
        for line in read(p).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in DEFAULTS:
                try:
                    words = shlex.split(value, comments=True)
                except ValueError:
                    raise UpdateError("%s: cannot parse %s" % (p, line))
                conf[key] = words[0] if words else ""
    for key in ("BOAT_UPDATE_CLIENT_CERT", "BOAT_UPDATE_CLIENT_KEY"):
        if not conf[key]:
            default = path("state", "client.crt" if key.endswith("CERT") else "client.key")
            if os.path.exists(default):
                conf[key] = default
    return conf


# --- the link: which device, and is it metered ------------------------------

def default_device():
    r = run(["ip", "route", "show", "default"])
    m = re.search(r"\bdev (\S+)", r.stdout)
    return m.group(1) if m else None


def nm_metered(dev):
    try:
        r = run(["nmcli", "-g", "GENERAL.METERED,GENERAL.TYPE", "device", "show", dev])
    except FileNotFoundError:
        return "unknown", ""       # no NetworkManager: every link counts as metered
    if r.returncode != 0:
        return "unknown", ""
    lines = r.stdout.splitlines() + ["", ""]
    return lines[0].strip(), lines[1].strip()


def is_metered_value(v):
    # NetworkManager: yes, no, yes (guessed), no (guessed), unknown.
    return not v.startswith("no")


def link():
    """(device, metered, why). A VPN default route hides the link under it,
    so then any metered active device makes the answer metered."""
    dev = default_device()
    if not dev:
        return None, True, "no default route"
    value, kind = nm_metered(dev)
    if kind in ("wireguard", "tun", "vpn", "ip-tunnel"):
        r = run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"])
        for line in r.stdout.splitlines():
            d, _, st = line.rpartition(":")
            if st == "connected" and d != dev and is_metered_value(nm_metered(d)[0]):
                return dev, True, "%s is metered (behind the VPN on %s)" % (d, dev)
        return dev, False, "VPN on %s, no metered link under it" % dev
    return dev, is_metered_value(value), "%s: Metered=%s" % (dev, value or "unknown")


# --- the channel ------------------------------------------------------------

def curl_base(conf, dev=None):
    cmd = ["curl", "--silent", "--show-error", "--fail", "--proto", "=https",
           "--max-redirs", "0", "--connect-timeout", "30"]
    if conf["BOAT_UPDATE_CLIENT_CERT"]:
        cmd += ["--cert", conf["BOAT_UPDATE_CLIENT_CERT"]]
    if conf["BOAT_UPDATE_CLIENT_KEY"]:
        cmd += ["--key", conf["BOAT_UPDATE_CLIENT_KEY"]]
    if dev:
        cmd += ["--interface", dev]
    return cmd


def curl_capped(cmd, out, limit, append=False):
    """Run curl with its body on stdout, and copy at most `limit` bytes of it
    to `out`. curl 7.82 (kirkstone) applies --max-filesize only to a
    Content-Length it is told; a chunked body is never cut off. So the cap
    is enforced here, and curl is killed the moment a server sends more.
    Returns (curl exit status, HTTP status, curl's messages, over the cap)."""
    with tempfile.TemporaryFile(mode="w+") as err, open(out, "ab" if append else "wb") as f:
        proc = subprocess.Popen(cmd + ["--write-out", "%{stderr}\nHTTP_STATUS=%{http_code}\n",
                                       "--output", "-"],
                                stdout=subprocess.PIPE, stderr=err)
        got, over = 0, False
        for block in iter(lambda: proc.stdout.read(1 << 16), b""):
            if got + len(block) > limit:
                f.write(block[:limit - got])
                over = True
                proc.kill()
                break
            f.write(block)
            got += len(block)
        proc.stdout.close()
        rc = proc.wait()
        f.flush()
        os.fsync(f.fileno())
        err.seek(0)
        text = err.read()
    m = re.search(r"HTTP_STATUS=(\d+)", text)
    return rc, m.group(1) if m else "", re.sub(r"\s*HTTP_STATUS=\d*\s*", " ", text).strip(), over


def channel_url(conf, name):
    base = conf["BOAT_UPDATE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise UpdateError("BOAT_UPDATE_URL must be an https:// URL (channel.conf), got %r" % base)
    return "%s/%s/%s" % (base, conf["BOAT_UPDATE_CHANNEL"], name)


def fetch_index(conf, dev):
    """Download index.p7 and return the verified JSON inside it."""
    with tempfile.TemporaryDirectory(prefix="boat-update-") as tmp:
        p7 = os.path.join(tmp, "index.p7")
        out = os.path.join(tmp, "index.json")
        rc, code, err, over = curl_capped(
            curl_base(conf, dev) + ["--max-time", "120", "-H", "Cache-Control: no-cache",
                                    channel_url(conf, "index.p7")], p7, INDEX_MAX)
        if over:
            raise UpdateError("the index is larger than %d bytes - refusing it" % INDEX_MAX)
        if rc != 0:
            raise UpdateError("cannot fetch the index: curl exit %d (HTTP %s): %s" % (rc, code or "-", err))
        # Every flag is load-bearing (plan 5.10): only our CA, never the
        # system store; the binary content as signed; the emailProtection
        # signer; no time check on a board without an RTC.
        signer = os.path.join(tmp, "signer.pem")
        r = run(["openssl", "cms", "-verify", "-inform", "DER", "-binary",
                 "-in", p7, "-out", out, "-signer", signer, "-CAfile", path("ca"),
                 "-no-CApath", "-no-CAstore", "-purpose", "smimesign", "-no_check_time"])
        if r.returncode != 0:
            raise UpdateError("the index's signature does not verify against %s: %s"
                              % (path("ca"), r.stderr.strip().splitlines()[-1:] or ""))
        # -purpose smimesign also passes a signer with no extended key usage
        # at all; the index-signing certificate must name emailProtection.
        r = run(["openssl", "x509", "-in", signer, "-noout", "-ext", "extendedKeyUsage"])
        if r.returncode != 0 or "E-mail Protection" not in r.stdout:
            raise UpdateError("the index is signed by a certificate without the emailProtection "
                              "extended key usage - not an index-signing certificate")
        try:
            return json.loads(read(out))
        except ValueError:
            raise UpdateError("the signed index is not valid JSON")


def validate_index(idx):
    """Field types and shapes; raises UpdateError naming the bad field."""
    if not isinstance(idx, dict):
        raise UpdateError("the index is not a JSON object")
    need = {"channel": str, "serial": int, "compatible": str, "version": str,
            "l4t": str, "bundle": str, "size": int, "sha256": str, "published": str}
    for key, kind in need.items():
        if not isinstance(idx.get(key), kind) or isinstance(idx.get(key), bool):
            raise UpdateError("index field %r is missing or not a %s" % (key, kind.__name__))
    if not BUNDLE_RE.match(idx["bundle"]) or ".." in idx["bundle"]:
        raise UpdateError("index names an unacceptable bundle file %r" % idx["bundle"])
    if not SHA256_RE.match(idx["sha256"]):
        raise UpdateError("index sha256 is not 64 lower-case hex digits")
    if idx["size"] <= 0 or not 0 <= idx["serial"] < SERIAL_MAX:
        raise UpdateError("index size/serial out of range")
    try:
        parse_version(idx["version"])
    except ValueError:
        raise UpdateError("index version %r is not a version" % idx["version"])
    if not isinstance(idx.get("requires_reflash", False), bool):
        raise UpdateError("index field 'requires_reflash' is not a boolean")
    notes = idx.get("notes", "")
    if not isinstance(notes, str):
        raise UpdateError("index field 'notes' is not a string")
    idx["notes"] = printable(notes, keep_newlines=True)[:NOTES_MAX]
    for key in ("published", "l4t", "compatible", "version", "channel"):
        if printable(idx[key]) != idx[key]:
            raise UpdateError("index field %r holds control characters" % key)


def printable(text, keep_newlines=False):
    """text without C0/C1 control characters and DEL - nothing an index
    says can drive the terminal it is shown on."""
    return "".join(c for c in text if (c == "\n" and keep_newlines)
                   or (c >= " " and not "\x7f" <= c <= "\x9f"))


def index_hash(idx):
    return hashlib.sha256(json.dumps(idx, sort_keys=True).encode()).hexdigest()


def decide(idx, conf, state, booted_version, other_version, compatible, l4t, failed):
    """What to do about a verified, well-formed index: returns
    (offer, message). offer is the index when its bundle should be fetched,
    else None. Pure, so it can be tested on its own."""
    if idx["channel"] != conf["BOAT_UPDATE_CHANNEL"]:
        raise UpdateError("the index is for channel %r, this board follows %r - refusing it"
                          % (idx["channel"], conf["BOAT_UPDATE_CHANNEL"]))
    # Per channel: switching to a channel that last published earlier than
    # the one left must not look like a replay.
    seen = (state.get("serials") or {}).get(idx["channel"], -1)
    # The same index again (a re-check while a download is under way) is
    # fine; anything else must be newer than everything accepted so far.
    same = idx["serial"] == seen and index_hash(idx) == state.get("index_hash")
    if idx["serial"] <= seen and not same:
        raise UpdateError("the index's serial %d is not above %d, the highest accepted - "
                          "a replayed or rolled-back index" % (idx["serial"], seen))
    if idx["compatible"] != compatible:
        raise UpdateError("the index is for %r, this board is %r" % (idx["compatible"], compatible))
    v = idx["version"]
    try:
        is_newer = newer(v, booted_version)
    except ValueError:
        is_newer = True       # the running release is not a version: anything is newer
    if not is_newer:
        return None, "up to date: the channel offers %s, this slot runs %s" % (v, booted_version)
    if v in failed:
        return None, "%s failed a trial on this board - not offered again" % v
    if v == other_version:
        return None, "%s is already in the other slot - 'boat-update rollback' switches to it" % v
    given_up = (state.get("given_up") or {}).get(idx["sha256"])
    if given_up and same:
        return None, ("%s was given up: %s - it is fetched again when the channel publishes "
                      "a new index" % (v, given_up))
    if idx["l4t"] != l4t:
        if idx.get("requires_reflash"):
            return None, "%s is a BSP bump (L4T %s): reflash at the dock (scripts/05-flash-nvme.sh)" % (v, idx["l4t"])
        raise UpdateError("the index offers %s for L4T %s, this board runs %s" % (v, idx["l4t"], l4t))
    return idx, "%s is available (this slot runs %s)" % (v, booted_version)


# --- gates ------------------------------------------------------------------

def clock_synced():
    r = run(["chronyc", "-n", "tracking"])
    if r.returncode == 0:
        return re.search(r"^Leap status\s*:\s*Normal", r.stdout, re.M) is not None
    r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return r.stdout.strip() == "yes"


def gates(for_install=False):
    """Refusals common to every verb that changes anything."""
    slot = booted_slot()
    if not slot:
        raise UpdateError("this boot did not come through GRUB (no rauc.slot=) - reboot first")
    env = grubenv()
    if env is None:
        raise UpdateError("grubenv is not readable - reboot so boat-grubenv-guard can rebuild it")
    if env.get("%s_COMMITTED" % slot) != "1" or cmdline().get("boat.trial") == "1":
        raise UpdateError("slot %s is still on trial - let it commit, or roll back, first" % slot)
    if not data_mounted():
        raise UpdateError("/data is not mounted from DATA - no updates without it")
    if not for_install and not clock_synced():
        raise UpdateError("the clock is not synchronised yet (chrony) - TLS cannot work before it is")
    return slot, env


@contextlib.contextmanager
def locked():
    os.makedirs(os.path.dirname(PATHS["lock"]), exist_ok=True)
    with open(PATHS["lock"], "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError("another boat-update is running")
        yield


# --- verbs ------------------------------------------------------------------

def do_check(conf, state, quiet=False):
    if not conf["BOAT_UPDATE_URL"]:
        raise UpdateError("no update channel configured (BOAT_UPDATE_URL in channel.conf)")
    slot, _ = gates()
    dev, metered, why = link()
    idx = fetch_index(conf, dev)
    validate_index(idx)
    slots = rauc_slots()
    offer, message = decide(idx, conf, state, running_release(),
                            slot_version(other(slot), slots, may_mount=True), system_compatible(),
                            running_l4t(), failed_versions())
    if index_hash(idx) != state.get("index_hash"):
        # A new index: what was given up under the old one is tried again.
        state.pop("given_up", None)
        state.pop("refetch", None)
    state.setdefault("serials", {})[idx["channel"]] = idx["serial"]
    state.pop("serial", None)
    state["index_hash"] = index_hash(idx)
    state["last_check"] = int(time.time())
    state["published"] = idx["published"]
    state["channel"] = conf["BOAT_UPDATE_CHANNEL"]
    state["message"] = message
    state["notes"] = idx["notes"]
    if offer:
        prev = state.get("offer") or {}
        if prev.get("bundle") != offer["bundle"]:
            # Content-addressed names: a different name is a different file.
            discard_downloads(keep=None)
        state["offer"] = {k: offer[k] for k in ("version", "bundle", "size", "sha256", "l4t")}
    else:
        state.pop("offer", None)
    save_state(state)
    if not quiet:
        say(message)
    return offer


def downloads_dir():
    return path("state", "downloads")


def discard_downloads(keep):
    d = downloads_dir()
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        if f != keep and f != (keep or "") + ".part":
            os.unlink(os.path.join(d, f))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def give_up(state, offer, why):
    """Stop fetching this bundle - not this version: a transfer or
    publishing fault is not a failed trial. A new index tries it again."""
    state.setdefault("given_up", {})[offer["sha256"]] = why
    state.pop("offer", None)
    save_state(state)
    raise UpdateError("%s: %s - given up until the channel publishes a new index"
                      % (offer["version"], why))


def refetch(state, offer, why):
    counts = state.setdefault("refetch", {})
    counts[offer["sha256"]] = counts.get(offer["sha256"], 0) + 1
    if counts[offer["sha256"]] > MAX_REFETCH:
        give_up(state, offer, "%s, %d times" % (why, MAX_REFETCH + 1))
    warn("%s - starting the download of %s over (%d of %d)"
         % (why, offer["version"], counts[offer["sha256"]], MAX_REFETCH))
    save_state(state)


def do_download(conf, state, allow_metered=False, windows=None):
    """Run download windows until the offered bundle is ready, the link may
    not be used, or a window makes no progress. Returns True when ready."""
    offer = state.get("offer")
    if not offer:
        say("nothing to download - %s" % state.get("message", "run 'boat-update check'"))
        return False
    final = os.path.join(downloads_dir(), offer["bundle"])
    part = final + ".part"
    if ready(state):
        say("%s is ready - 'boat-update install' installs it" % offer["version"])
        return True
    discard_downloads(keep=offer["bundle"])
    os.makedirs(downloads_dir(), exist_ok=True)
    stalled = 0
    n = 0
    while windows is None or n < windows:
        n += 1
        gates()
        dev, metered, why = link()
        if metered and conf["BOAT_UPDATE_METERED"] != "yes" and not allow_metered:
            state["waiting"] = "metered link (%s)" % why
            save_state(state)
            say("%s available, waiting for an unmetered link (%s)" % (offer["version"], why))
            return False
        state.pop("waiting", None)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > offer["size"]:
            os.unlink(part)
            refetch(state, offer, "the partial download is larger than the bundle")
            continue
        free = shutil.disk_usage(PATHS["data"]).free
        if free < offer["size"] - have + FREE_FLOOR:
            raise UpdateError("/data has %d MiB free; this download needs %d MiB plus 2 GiB to spare"
                              % (free >> 20, (offer["size"] - have) >> 20))
        if have < offer["size"]:
            cmd = curl_base(conf, dev) + [
                "--max-time", str(WINDOW_SECONDS),
                "--speed-limit", str(STALL_BYTES), "--speed-time", str(STALL_SECONDS)]
            if have:
                cmd += ["--continue-at", str(have)]
            if conf["BOAT_UPDATE_RATE_LIMIT"]:
                cmd += ["--limit-rate", conf["BOAT_UPDATE_RATE_LIMIT"]]
            rc, code, err, over = curl_capped(cmd + [channel_url(conf, offer["bundle"])],
                                              part, offer["size"] - have, append=True)
            now = os.path.getsize(part) if os.path.exists(part) else 0
            if over:
                # More than the index's size: never keep, never resume it.
                os.unlink(part)
                refetch(state, offer, "the server sent more than the %d bytes the index promised"
                        % offer["size"])
                continue
            if code == "416":
                # 416: our .part is not a prefix of what the server has.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part)
                refetch(state, offer, "the server refused to resume (HTTP 416)")
                continue
            if rc == 33:
                # The server answered the Range request with the whole file:
                # it cannot resume. Start again from the beginning - a
                # server fault, not a reason to count this against the bundle.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part)
                warn("the server does not resume downloads (it ignored the Range request) - "
                     "starting over from the beginning")
                stalled += 1
                if stalled >= 2:
                    say("no progress in two windows - stopping until the next run")
                    return False
                continue
            if rc not in (0, 28):
                warn("download window ended: curl exit %d (HTTP %s) %s" % (rc, code or "-", err))
            if now > have:
                stalled = 0
            else:
                stalled += 1
            say("%s: %d of %d MiB" % (offer["bundle"], now >> 20, offer["size"] >> 20))
            if now < offer["size"]:
                if stalled >= 2:
                    say("no progress in two windows - stopping until the next run")
                    return False
                continue
        # Complete: check it is the file the index promised, and that RAUC
        # will take it, before calling it ready.
        if sha256_file(part) != offer["sha256"]:
            os.unlink(part)
            refetch(state, offer, "the download's SHA-256 does not match the index")
            continue
        os.replace(part, final)
        problem = verify_bundle(final, offer["version"])
        if problem:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(final)
            give_up(state, offer, problem)
        state["ready"] = {"bundle": offer["bundle"], "version": offer["version"], "sha256": offer["sha256"]}
        save_state(state)
        say("%s is ready - 'boat-update install' installs it" % offer["version"])
        return True
    return False


def verify_bundle(bundle, want_version):
    """RAUC checks the signature; it does not compare 'compatible' itself.
    Returns what is wrong with the bundle, or None. Downloading the same
    signed bytes again cannot fix it, so the caller gives the bundle up."""
    r = run(["rauc", "info", "--output-format=json", bundle])
    problem = None
    info = {}
    if r.returncode != 0:
        problem = "RAUC rejects it: %s" % r.stderr.strip()
    else:
        try:
            info = json.loads(r.stdout)
        except ValueError:
            problem = "'rauc info' output is unreadable"
    if not problem and info.get("compatible") != system_compatible():
        problem = "it is for %r, this board is %r" % (info.get("compatible"), system_compatible())
    if not problem and info.get("version") != want_version:
        problem = "it is version %r, the index said %r" % (info.get("version"), want_version)
    return problem


def ready(state):
    r = state.get("ready")
    if not r:
        return None
    p = os.path.join(downloads_dir(), r["bundle"])
    return p if os.path.exists(p) else None


def trial_outcome(state):
    """Record a trial that ended without boat-commit saying so - a kernel
    panic, a hang, a power cut: the board is back on the other slot, and the
    slot that was installed is uncommitted and either marked bad or out of
    attempts. That version is not offered again."""
    inst = state.get("installed")
    slot = booted_slot()
    if not inst or not slot:
        return
    env = grubenv() or {}
    t = inst["slot"]
    if env.get("%s_COMMITTED" % t) == "1":
        state.pop("installed", None)            # it passed (whether running or not)
    elif slot != t and (env.get("%s_OK" % t) == "0" or env.get("%s_TRY" % t, "0") not in ("", "0")):
        if inst["version"] not in failed_versions():
            mark_failed(inst["version"])
            warn("the trial of %s in slot %s did not complete - it is not offered again"
                 % (inst["version"], t))
        state.pop("installed", None)
    else:
        return
    save_state(state)


def housekeeping(state):
    """A ready bundle that is now running (committed), failed its trial or is
    no newer than the running release is no longer needed: the old slot is
    the rollback, not the file."""
    trial_outcome(state)
    r = state.get("ready")
    if not r:
        return
    slot = booted_slot()
    env = grubenv() or {}
    done = (r["version"] == running_release() and slot and env.get("%s_COMMITTED" % slot) == "1")
    try:
        stale = not newer(r["version"], running_release())
    except ValueError:
        stale = False
    if done or stale or r["version"] in failed_versions():
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(downloads_dir(), r["bundle"]))
        state.pop("ready", None)
        if (state.get("offer") or {}).get("version") == r["version"]:
            state.pop("offer", None)
        save_state(state)


def ready_to_install(state):
    """The downloaded bundle, if it is still the one to install: the current
    offer, newer than the running release and not a version that failed."""
    housekeeping(state)
    r, offer = state.get("ready"), state.get("offer") or {}
    bundle = ready(state)
    if not bundle or r.get("sha256") != offer.get("sha256"):
        return None
    if r["version"] in failed_versions():
        return None
    try:
        if not newer(r["version"], running_release()):
            return None
    except ValueError:
        pass
    return bundle


def resolve_bundle(state, bundle):
    if bundle is None:
        bundle = ready_to_install(state)
        if not bundle:
            raise UpdateError("no bundle is ready - 'boat-update check' and 'download' first, or name one")
    bundle = os.path.abspath(bundle)
    if not os.path.isfile(bundle):
        raise UpdateError("%s does not exist" % bundle)
    return bundle


def do_install(conf, state, bundle=None, reboot=False):
    slot, _ = gates(for_install=True)
    if os.environ.get("BOAT_UPDATE_INNER") != "1":
        # Run the install as its own systemd unit, so a dropped SSH session
        # cannot kill this wrapper while RAUC carries on without it. The
        # lock is taken here too: to fail fast if a download holds it, and
        # because choosing the bundle tidies the state.
        with locked():
            bundle = resolve_bundle(load_state(), bundle)
        unit = "boat-update-install"
        run(["systemctl", "reset-failed", unit])
        args = ["systemd-run", "--unit=" + unit, "--setenv=BOAT_UPDATE_INNER=1",
                "--description=boat-update install",
                sys.argv[0], "install", bundle] + (["--reboot"] if reboot else [])
        r = run(args)
        if r.returncode != 0:
            raise UpdateError("could not start the install unit: %s" % r.stderr.strip())
        say("installing in the background (unit %s); following its log - "
            "losing this session does not stop it" % unit)
        follow = subprocess.Popen(["journalctl", "-f", "-o", "cat", "-u", unit])
        try:
            while run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
                time.sleep(2)
        finally:
            time.sleep(1)
            follow.terminate()
        # A unit that failed stays loaded with its result (no --collect); one
        # that succeeded is gone, and reads as Result=success.
        result = run(["systemctl", "show", "-p", "Result", "--value", unit]).stdout.strip()
        if result not in ("", "success"):
            run(["systemctl", "reset-failed", unit])
            raise UpdateError("the install failed (%s) - see above; the board stays on this slot" % result)
        return
    bundle = resolve_bundle(state, bundle)
    info = verify_bundle_info_only(bundle)
    say("installing %s (%s) into the other slot" % (bundle, info.get("version", "?")))
    # Sleep and shutdown are held off for the whole write (plan 5.6); the
    # trial's own inhibitor takes over after the reboot.
    r = subprocess.run(["systemd-inhibit", "--what=sleep:shutdown", "--who=boat-update",
                        "--why=writing the other A/B slot", "--mode=block",
                        "rauc", "install", bundle])
    if r.returncode != 0:
        raise UpdateError("RAUC did not install %s (exit %d) - the board stays on this slot" % (bundle, r.returncode))
    # Remembered so a trial that never reaches boat-commit is still recorded.
    state["installed"] = {"version": info.get("version", "?"), "slot": other(slot)}
    save_state(state)
    say("installed: the other slot boots as a trial on the next reboot, and falls back if it fails")
    if reboot:
        say("rebooting now")
        run(["systemctl", "reboot"])


def verify_bundle_info_only(bundle):
    r = run(["rauc", "info", "--output-format=json", bundle])
    if r.returncode != 0:
        raise UpdateError("RAUC rejects %s: %s" % (bundle, r.stderr.strip()))
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def do_rollback(yes=False):
    slot = booted_slot()
    if not slot:
        raise UpdateError("this boot did not come through GRUB - nothing to roll back from")
    env = grubenv() or {}
    o = other(slot)
    if env.get("%s_OK" % o) != "1":
        raise UpdateError("slot %s is not usable (%s_OK=%s) - there is nothing to roll back to"
                          % (o, o, env.get("%s_OK" % o, "?")))
    say("this switches the board to slot %s (%s) and reboots"
        % (o, slot_version(o, rauc_slots()) or "unknown version"))
    if not yes:
        sys.stdout.write("Type 'yes' to go ahead: ")
        sys.stdout.flush()
        if sys.stdin.readline().strip() != "yes":
            raise UpdateError("not confirmed - nothing changed")
    r = run(["rauc", "status", "mark-active", "other"])
    if r.returncode != 0:
        raise UpdateError("RAUC could not switch slots: %s" % r.stderr.strip())
    run(["systemctl", "reboot"])


def do_debug_hold(on):
    if os.geteuid() != 0:
        raise UpdateError("run as root")
    with locked():
        grubenv_set(BOAT_STAY="1" if on else "0")
    say("BOAT_STAY=%d: %s" % (on, "a failure now stops for inspection instead of resetting (boat.stay=1)"
                               if on else "failures reset the board again"))


def do_history():
    for name in ("history", "last-result", "failed-versions"):
        text = read(path("state", name)).rstrip()
        if text:
            print("== %s" % name)
            print(text)


def do_auto(conf, state):
    """What the timer and the network/sleep triggers run."""
    housekeeping(state)
    policy = conf["BOAT_UPDATE_AUTO"]
    if policy == "off":
        return
    stamp = state.get("last_auto", 0)
    if time.time() - stamp < AUTO_MIN_INTERVAL and not state.get("offer"):
        return
    state["last_auto"] = int(time.time())
    save_state(state)
    offer = do_check(conf, state)
    if offer and policy == "download":
        do_download(conf, state)


def age(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "%d s ago" % s


def do_status(conf, state):
    slot = booted_slot()
    env = grubenv()
    slots = rauc_slots()
    print("A/B update status")
    if not slot:
        print("  booted OUTSIDE GRUB (no rauc.slot=): updates are refused until a reboot through GRUB")
    else:
        trial = env is not None and env.get("%s_COMMITTED" % slot) != "1"
        print("  running   slot %s, %s, release %s" % (slot, "ON TRIAL" if trial else "committed", running_release()))
        o = other(slot)
        ok = (env or {}).get("%s_OK" % o) == "1"
        print("  other     slot %s, %s, release %s" % (o, "usable" if ok else "not usable",
                                                       slot_version(o, slots) or "unknown"))
    if env is None:
        print("  ALERT     grubenv is unreadable")
    for name, text in (("grubenv-repaired", "grubenv was torn and rebuilt this boot"),
                       ("health-alert", "the running slot is committed but unhealthy: ")):
        p = path("run_ab", name)
        if os.path.exists(p):
            print("  ALERT     %s%s" % (text, read(p).strip() if name == "health-alert" else ""))
    last = read(path("state", "last-result")).strip()
    if last:
        print("  last      %s" % last)
    if (env or {}).get("BOAT_STAY") == "1":
        print("  debug-hold is ON: failures stop for inspection instead of resetting")
    print("OTA")
    if not conf["BOAT_UPDATE_URL"]:
        print("  no channel configured (BOAT_UPDATE_URL)")
        return
    print("  channel   %s at %s (automatic: %s, metered links: %s)"
          % (conf["BOAT_UPDATE_CHANNEL"], conf["BOAT_UPDATE_URL"], conf["BOAT_UPDATE_AUTO"], conf["BOAT_UPDATE_METERED"]))
    print("  checked   %s; channel published %s" % (age(state.get("last_check")), state.get("published", "?")))
    if state.get("message"):
        print("  result    %s" % state["message"])
    offer = state.get("offer")
    if offer:
        r = ready(state)
        if r:
            print("  READY     %s - 'boat-update install --reboot' installs it" % offer["version"])
        else:
            part = os.path.join(downloads_dir(), offer["bundle"] + ".part")
            have = os.path.getsize(part) if os.path.exists(part) else 0
            print("  download  %s: %d of %d MiB%s" % (offer["version"], have >> 20, offer["size"] >> 20,
                                                      ", waiting: " + state["waiting"] if state.get("waiting") else ""))
    if state.get("notes"):
        print("  notes     " + state["notes"].replace("\n", "\n            "))


def main(argv):
    args = argv[1:] or ["status"]
    verb, rest = args[0], args[1:]
    if verb in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[0].split("\n", 1)[1])
        return 0
    try:
        conf = load_conf()
        state = load_state() if os.path.isdir(PATHS["state"]) else {}
        if verb == "status":
            do_status(conf, state)
        elif verb == "history":
            do_history()
        elif verb == "install" and os.environ.get("BOAT_UPDATE_INNER") != "1":
            if os.geteuid() != 0:
                raise UpdateError("run as root")
            # The outer wrapper only starts and follows the install unit;
            # the unit's own run takes the lock.
            files = [a for a in rest if not a.startswith("--")]
            if len(files) > 1:
                raise UpdateError("install takes at most one bundle")
            do_install(conf, state, files[0] if files else None, reboot="--reboot" in rest)
        elif verb in ("check", "download", "auto", "install"):
            if os.geteuid() != 0:
                raise UpdateError("run as root")
            with locked():
                state = load_state()
                if verb == "check":
                    do_check(conf, state)
                elif verb == "download":
                    do_download(conf, state, allow_metered="--allow-metered" in rest)
                elif verb == "auto":
                    do_auto(conf, state)
                else:
                    files = [a for a in rest if not a.startswith("--")]
                    if len(files) > 1:
                        raise UpdateError("install takes at most one bundle")
                    do_install(conf, state, files[0] if files else None, reboot="--reboot" in rest)
        elif verb == "rollback":
            do_rollback(yes="--yes" in rest)
        elif verb == "debug-hold":
            if rest not in (["on"], ["off"]):
                raise UpdateError("usage: boat-update debug-hold on|off")
            do_debug_hold(rest == ["on"])
        else:
            raise UpdateError("unknown verb %r - see boat-update --help" % verb)
    except UpdateError as e:
        print("boat-update: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
