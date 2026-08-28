# `clients/typescript` — wake and sleep the boat from TypeScript

A complete, dependency-free TypeScript implementation of the boat power
protocol: the sender (`wake`, `sleep`) **and** the receiver (`listen`). Runtime
dependencies: none. `node:dgram`, `node:crypto`, `node:net` only; TypeScript is
a devDependency for the build.

Ships both ESM and CommonJS, because the motivating consumer — a SignalK plugin
— is usually CommonJS, and CJS cannot `require()` an ESM-only package at all.

```bash
npm install
npm test     # builds, then runs the unit, conformance and live-socket tests
```

## What runs where

| | |
|---|---|
| **On the boat** | `boat-sleepd.py`, installed by meta-boat's `boat-sleep-listener` recipe. Unchanged by anything here. |
| **This package** | The client, for anything that wants to wake or sleep the boat — and a receiver, for embedding in a Node process of your own. |

Nothing here is installed by the Yocto image. The image already carries
python3; adding a ~40 MB Node runtime to replace a working 300-line daemon
would be a poor trade. `listen()` is here for when the receiver belongs inside
a Node service you are *already* running.

## The two directions are not symmetrical

They look it from outside. They are not, and the asymmetry is the design:

**`wake()` sends a Wake-on-LAN magic packet.** The board is off. Nothing on it
is running. The NIC itself, still powered in a low-power state, matches the
frame in firmware and asserts PME. There is no authentication because the
format has nowhere to put any — the payload is six `0xFF` bytes then the target
MAC sixteen times.

**`sleep()` sends a signed datagram to a service on the running board.** There
is no hardware "sleep on LAN" — no NIC filter, no ACPI primitive, nothing in
the WoL spec — so this has to be software on a running system. And software
that suspends the boat's navigation computer had better know who is asking: a
magic packet is forgeable by anyone who has seen one frame from the board, so
an unauthenticated "suspend now" port means any guest on the marina wifi can
black out the helm at will.

Hence HMAC-SHA256 over a timestamp and a nonce, keyed by the board's
`/etc/boat-sleep.key`.

## Using it

Cmd-line example
```
BOAT_HOST=192.168.0.43 BOAT_SLEEP_KEY_FILE=~/.config/boat/sleep.key node dist/esm/example.js sleep
BOAT_HOST=192.168.0.43 BOAT_MAC=48:b0:2d:15:e1:11 BOAT_BROADCAST=192.168.0.255 node dist/esm/example.js wake
```

```ts
import { wake, sleep, waitForPort } from '@boat/power-client';

await wake({ mac: '48:b0:2d:15:e1:11', broadcast: '192.168.0.255' });
await waitForPort('192.168.0.43', 22, 'open', 90_000);

await sleep({ host: '192.168.0.43', key: readFileSync('sleep.key', 'utf8') });
```

### In a SignalK plugin

```js
const { sleep } = require('@boat/power-client');
const { listen } = require('@boat/power-client/listener');

module.exports = function (app) {
  let listener;
  return {
    id: 'boat-power',
    name: 'Boat power',
    start: async (settings) => {
      listener = await listen({
        key: settings.sleepKey,
        onSleep: (from) => app.debug(`suspend requested by ${from.address}`),
        // Structured events rather than preformatted strings, so the host
        // logs them the way it wants to.
        logger: (e) => (e.level === 'error' ? app.error(e.msg) : app.debug(e.msg)),
      });
    },
    stop: async () => listener?.close(),
  };
};
```

`listen()` never binds a port you did not ask for, never writes to stdout, and
routes every line through `logger` — the three things that make a library
unpleasant to embed.

## Wire format (protocol v1, 60 bytes, big-endian)

| offset | size | field |
|---|---|---|
| 0 | 8 | magic `BOATSLP1` |
| 8 | 1 | version `0x01` |
| 9 | 1 | opcode `0x01` = sleep |
| 10 | 2 | reserved, zero |
| 12 | 8 | timestamp, unix seconds |
| 20 | 8 | nonce, random |
| 28 | 32 | HMAC-SHA256(key, bytes 0..27) |

The receiver checks the MAC **first and in constant time**, *then* the
timestamp against a 30-second window, *then* the nonce against everything seen
inside that window. That order is load-bearing: admit the nonce before checking
the signature and anyone who can guess a nonce can burn the one a real sender
is about to use — denying exactly the command you need when the boat is
unreachable. There is a test for it.

## How the two implementations are kept honest

`src/vectors.json` holds 19 conformance vectors — packets and expected verdicts
— **generated from `boat-sleepd.py`**, the implementation that actually ships.
It is the authority; this package is what conforms.

This package has also driven a real Xavier NX: `sleep()` suspended the board to
SC7 and `wake()` brought it back. The vectors remain the thing that catches
divergence *in CI*, where no boat is available — but the wire format itself is
confirmed end to end against real hardware, not only against them.

They exist because two implementations that only ever test themselves will each
pass their own suite forever and still disagree on the wire — and that
disagreement surfaces as a sleep command that silently does nothing, on a boat,
which is the worst possible place to find it. The vectors cover every refusal
path (bad signature, bad magic, bad version, wrong length, stale timestamp,
replayed nonce, unknown opcode), both window edges at exactly ±30s, and a real
Wake-on-LAN magic packet as input.

Cases run **in order against one shared replay cache** — case 2 is case 1's
bytes a second time, and is only a replay because of that.

Regenerate after any protocol change:

```bash
scripts/gen-sleep-vectors.py    # from the repo root
```

If regenerating changes an expected verdict, you changed the protocol — whether
or not you meant to.

## Deliberate divergence from `boat-sleepd`

One, and it is not on the wire: `ReplayCache` here is bounded by entry count as
well as by window. Only a key holder can grow that cache, so window-only
bounding is not an unauthenticated memory attack — but "nobody can exhaust our
heap" beats "only an insider can", it costs one comparison, and in a
long-lived Node process shared with other things an unbounded `Map` is a worse
neighbour than it is in a dedicated daemon.

## Getting the key

Each board generates its own on first boot — nothing is baked into the image,
because a secret shared by every board built from one image is not a secret.

```bash
ssh root@boat cat /etc/boat-sleep.key > ~/.config/boat/sleep.key
chmod 0600 ~/.config/boat/sleep.key
```

Read it from a **file**, not an environment variable: an env var is readable
from `/proc/<pid>/environ`, is inherited by every child process, and ends up in
shell history and CI logs. `src/example.ts` does it the right way.

## What these functions can and cannot tell you

`wake()` and `sleep()` resolve once the packet is away. Neither can tell you it
worked:

- A magic packet is unacknowledged, and the board is not running any software
  that could answer.
- The listener deliberately answers **nothing** — valid or not — so it cannot
  be used as a reflection amplifier and a prober cannot distinguish "bad
  signature" from "replayed nonce". There is a test asserting it stays silent.

`waitForPort()` is the honest way to find out. Note that a board merely
dropping the port — a firewall — looks identical to one that is asleep. When a
sleep request seems to vanish, the reason is in the board's journal:

```bash
ssh root@boat journalctl -u boat-sleep-listener -n 20
```

The most common cause is not a bug: `boat-sleep` refuses to suspend a board
that nothing can wake again, which is it working correctly.
