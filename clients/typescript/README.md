# `clients/typescript` — wake and sleep the boat from TypeScript

Dependency-free reference implementation of both directions. `node:dgram` and
`node:crypto`, nothing else; TypeScript is a devDependency for the build only.

```bash
npm install
npm run build
npm test              # 8 unit tests
node cross-check.mjs  # runs the REAL target daemon and checks it accepts us
```

```ts
import { wake, sleep } from '@boat/power-client';

await wake({ mac: '48:b0:2d:11:22:33', broadcast: '192.168.1.255' });
await sleep({ host: '192.168.1.42', key: readFileSync('sleep.key', 'utf8') });
```

## The two directions are not symmetrical

They look it from the outside. They are not, and the asymmetry is the whole
design:

**`wake()` sends a Wake-on-LAN magic packet.** The board is off. Nothing on it
is running. The NIC itself, still powered in a low-power state, pattern-matches
the frame in firmware and asserts PME to bring the host up. There is no
authentication because there is nowhere in the format to put any — the payload
is defined as six `0xFF` bytes followed by the target MAC sixteen times.

**`sleep()` sends a signed datagram to a service on the running board.** There
is no hardware "sleep on LAN" — no NIC filter, no ACPI primitive, nothing in
the WoL spec — so this cannot be a magic packet. It has to be software on a
running system, and software on a running system that suspends the boat's
navigation computer had better know who is asking. A magic packet is forgeable
by anyone who has seen a single frame from the board.

So each sleep packet carries an HMAC-SHA256 over a timestamp and a nonce, keyed
by the board's `/etc/boat-sleep.key`.

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

The receiver checks the MAC first and in constant time, *then* the timestamp
against a 30-second window, *then* the nonce against everything it has seen
inside that window. That order matters: an unauthenticated packet must not be
able to burn a nonce the real sender is about to use, or steer the clock
comparison. `cross-check.mjs` proves the ordering holds against the real
daemon, not just against this file's own idea of it.

The authority on this format is
`layers/meta-boat/recipes-boat/power/files/boat-sleepd.py`. If the two ever
disagree, that one is right and `cross-check.mjs` will say so.

## Getting the key

Each board generates its own on first boot — nothing is baked into the image,
because a secret shared by every board built from one image is not a secret.

```bash
ssh root@boat cat /etc/boat-sleep.key > ~/.config/boat/sleep.key
chmod 0600 ~/.config/boat/sleep.key
```

Read it from a **file**, not an environment variable. An env var is readable
from `/proc/<pid>/environ`, is inherited by every child process, and ends up in
shell history and CI logs. `src/example.ts` does it the right way.

## What these functions can and cannot tell you

Both resolve once the packet is away. Neither can tell you it worked:

- A magic packet is unacknowledged, and the board is not running any software
  that could answer.
- The sleep listener deliberately answers **nothing** — valid or not — so it
  cannot be used as a reflection amplifier and a prober cannot distinguish "bad
  signature" from "replayed nonce".

`waitForPort()` is the honest way to find out: poll TCP 22 until it starts or
stops accepting. Note that a board merely dropping the port — a firewall —
looks identical to one that is asleep. When a sleep request seems to vanish,
the reason is in the board's journal:

```bash
ssh root@boat journalctl -u boat-sleep-listener -n 20
```

The most common cause is not a bug: `boat-sleep` refuses to suspend a board
that nothing can wake again, which is it working correctly.
