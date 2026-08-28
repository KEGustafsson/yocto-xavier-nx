#!/usr/bin/env python3
"""Regenerate the sleep-protocol conformance vectors from boat-sleepd.

    scripts/gen-sleep-vectors.py

Writes clients/typescript/src/vectors.json.

WHY THIS EXISTS
clients/typescript and layers/meta-boat/.../boat-sleepd.py implement the same
wire protocol in two languages. Two implementations that only ever test
themselves will pass their own suites forever and still disagree on the wire -
and that disagreement shows up as a sleep command that silently does nothing,
on a boat, which is the worst place to discover it.

These vectors are the contract between them. They are generated HERE, from
boat-sleepd, because boat-sleepd is what actually ships: it is the authority,
and the TypeScript side is what must conform. The TypeScript test suite replays
them and asserts the same verdict for every case.

Deliberately NOT a live cross-process check: this file runs python3, so it
lives in scripts/ with the rest of the repo tooling, leaving the TypeScript
package free of any Python dependency while keeping the guarantee that made
the live check worth having.
"""

import hashlib
import hmac
import importlib.util
import json
import pathlib
import struct
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DAEMON = REPO / "layers/meta-boat/recipes-boat/power/files/boat-sleepd.py"
OUT = REPO / "clients/typescript/src/vectors.json"

# A fixed, obviously-fake key. Vectors must be reproducible, so nothing here
# may be random - and nobody should ever mistake this for a real one.
KEY = "4b1e" * 16
NOW = 1_700_000_000
WINDOW = 30


def load_daemon():
    spec = importlib.util.spec_from_file_location("sleepd", DAEMON)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {DAEMON}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(reason):
    """Map boat-sleepd's prose refusals onto the names both sides share.

    Done once, here, rather than making either implementation match the
    other's wording: the wire format is the contract, not the log strings.
    """
    if reason is None:
        return "accept"
    for prefix, name in (
        ("wrong length", "wrong-length"),
        ("bad magic", "bad-magic"),
        ("unsupported protocol version", "bad-version"),
        ("bad signature", "bad-signature"),
        ("timestamp", "stale-timestamp"),
        ("replayed nonce", "replayed-nonce"),
        ("unknown opcode", "unknown-opcode"),
    ):
        if reason.startswith(prefix):
            return name
    sys.exit(f"unmapped refusal from boat-sleepd: {reason!r} - add it to canonical()")


def main():
    m = load_daemon()
    key = bytes.fromhex(KEY)

    def signed(**kw):
        return m.build_packet(key, **kw)

    def flip(packet, index):
        b = bytearray(packet)
        b[index] ^= 0x01
        return bytes(b)

    good = signed(timestamp=NOW, nonce=0x1122334455667788)
    bad_version_header = struct.pack(m.HEADER_FMT, m.MAGIC, 2, 1, 0, NOW, 0xD0)

    cases = [
        ("a valid request", good, None),
        ("the same packet again", good, "replay: identical bytes, second time"),
        ("wrong key", m.build_packet(bytes(32), timestamp=NOW, nonce=0xAAAA), None),
        ("flipped magic byte", flip(good, 0), None),
        ("flipped version byte", flip(good, 8), None),
        ("flipped timestamp byte", flip(good, 12), None),
        ("flipped nonce byte", flip(good, 20), None),
        ("flipped first mac byte", flip(good, 28), None),
        ("flipped last mac byte", flip(good, 59), None),
        ("truncated by one", good[:-1], None),
        ("one byte too long", good + b"\x00", None),
        ("empty datagram", b"", None),
        ("a real Wake-on-LAN magic packet",
         b"\xff" * 6 + b"\x48\xb0\x2d\x11\x22\x33" * 16,
         "the packet this protocol exists BECAUSE anyone can forge"),
        ("exactly at the window edge, behind", signed(timestamp=NOW - WINDOW, nonce=0xB0), None),
        ("exactly at the window edge, ahead", signed(timestamp=NOW + WINDOW, nonce=0xB1), None),
        ("one second past the window, behind", signed(timestamp=NOW - WINDOW - 1, nonce=0xB2), None),
        ("one second past the window, ahead", signed(timestamp=NOW + WINDOW + 1, nonce=0xB3), None),
        ("unknown opcode", signed(opcode=99, timestamp=NOW, nonce=0xC0), None),
        ("unsupported version",
         bad_version_header + hmac.new(key, bad_version_header, hashlib.sha256).digest(),
         None),
    ]

    # One shared cache, cases in order - that is what makes case 2 a replay of
    # case 1 rather than an isolated packet. The consumer must do the same.
    cache = m.ReplayCache(WINDOW)
    out = []
    for name, packet, note in cases:
        _opcode, reason = m.verify(packet, key, WINDOW, cache, NOW)
        entry = {"name": name, "packet": packet.hex(), "expect": canonical(reason)}
        if note:
            entry["note"] = note
        out.append(entry)

    doc = {
        "_comment": [
            "Conformance vectors for the boat sleep protocol (v1).",
            "GENERATED FROM boat-sleepd.py, which is what ships on the boat -",
            "it is the authority here, and the TypeScript side must conform.",
            "Cases run IN ORDER against ONE shared replay cache: case 2 is",
            "case 1's bytes a second time and expects replayed-nonce for that",
            "reason. Running them out of order or with a fresh cache each time",
            "silently weakens the suite rather than failing it.",
            "Regenerate with: scripts/gen-sleep-vectors.py",
        ],
        "key": KEY,
        "window": WINDOW,
        "now": NOW,
        "cases": out,
    }
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"{len(out)} vectors -> {OUT.relative_to(REPO)}")
    for entry in out:
        print(f"  {entry['expect']:16s} {entry['name']}")


if __name__ == "__main__":
    main()
