/**
 * Tests for the TypeScript receiver.
 *
 * Two kinds, and the first is the one that matters:
 *
 *  1. CONFORMANCE against vectors.json, which is generated from the Python
 *     boat-sleepd that actually ships on the boat. Two implementations that
 *     only test themselves will each pass forever and still disagree on the
 *     wire; these vectors are what stops that. Regenerate them with
 *     scripts/gen-sleep-vectors.py after any protocol change - and if that
 *     regeneration changes an expected verdict, you changed the protocol,
 *     whether or not you meant to.
 *
 *  2. Behaviour of listen() itself over a real socket: that it answers
 *     nothing, that it survives a throwing handler, that it binds.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createSocket } from 'node:dgram';

import { buildSleepPacket, buildMagicPacket } from './boat-power.js';
import { ReplayCache, listen, verify } from './listener.js';

interface Vectors {
  key: string;
  window: number;
  now: number;
  cases: { name: string; packet: string; expect: string; note?: string }[];
}

// readFileSync rather than a JSON import: import attributes are spelled
// differently across the Node versions this package supports, and a test
// harness is the last place worth a portability puzzle.
const HERE = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(
  readFileSync(join(HERE, 'vectors.json'), 'utf8'),
) as Vectors;

test('conformance: every vector from boat-sleepd gets the same verdict here', () => {
  assert.ok(vectors.cases.length >= 19, 'vectors.json looks truncated');

  // ONE cache, cases in order - the vectors were generated that way, and case
  // 2 is only a replay because case 1 ran first. A fresh cache per case would
  // still pass most of them while quietly testing something weaker.
  const cache = new ReplayCache(vectors.window);
  const failures: string[] = [];

  for (const [i, testCase] of vectors.cases.entries()) {
    const packet = Buffer.from(testCase.packet, 'hex');
    const result = verify(packet, vectors.key, vectors.window, cache, vectors.now);
    const got = result.ok ? 'accept' : (result.reason ?? 'undefined');
    if (got !== testCase.expect) {
      failures.push(
        `  [${i}] ${testCase.name}: boat-sleepd says ${testCase.expect}, we say ${got}`,
      );
    }
  }

  assert.equal(
    failures.length,
    0,
    `TypeScript and boat-sleepd disagree on the wire:\n${failures.join('\n')}`,
  );
});

test('conformance: the vectors actually cover the refusals, not just accepts', () => {
  const seen = new Set(vectors.cases.map((c) => c.expect));
  for (const reason of [
    'accept',
    'bad-signature',
    'bad-magic',
    'bad-version',
    'wrong-length',
    'stale-timestamp',
    'replayed-nonce',
    'unknown-opcode',
  ]) {
    assert.ok(seen.has(reason), `no vector exercises ${reason}`);
  }
});

test('a packet this package builds is accepted by this package', () => {
  const key = 'ab'.repeat(32);
  const cache = new ReplayCache(30);
  const now = 1_700_000_000;
  const packet = buildSleepPacket({ host: 'x', key, timestamp: now });
  assert.equal(verify(packet, key, 30, cache, now).ok, true);
});

test('a Wake-on-LAN magic packet is never a sleep request', () => {
  const key = 'ab'.repeat(32);
  const result = verify(
    buildMagicPacket('48:b0:2d:11:22:33'),
    key,
    30,
    new ReplayCache(30),
    1_700_000_000,
  );
  assert.equal(result.ok, false);
});

test('an unauthenticated packet cannot burn a nonce the real sender will use', () => {
  // The check order is the whole point: verify the MAC BEFORE admitting the
  // nonce. Get it backwards and anyone who can guess a nonce can deny the one
  // command you need when the boat is unreachable.
  const key = 'ab'.repeat(32);
  const now = 1_700_000_000;
  const cache = new ReplayCache(30);

  const real = buildSleepPacket({ host: 'x', key, timestamp: now });
  const forged = Buffer.from(real);
  forged[59]! ^= 0xff; // same nonce, broken signature

  assert.equal(verify(forged, key, 30, cache, now).reason, 'bad-signature');
  assert.equal(cache.size, 0, 'a forged packet must not reach the replay cache');
  assert.equal(verify(real, key, 30, cache, now).ok, true);
});

test('the replay cache is bounded by entry count as well as by window', () => {
  // Deliberate divergence from boat-sleepd, documented in listener.ts: only a
  // key holder can grow this cache, but "nobody can exhaust our heap" beats
  // "only an insider can", and it costs one comparison.
  const cache = new ReplayCache(3600, 8);
  for (let i = 0; i < 100; i++) {
    assert.equal(cache.checkAndAdd(BigInt(i), 1_700_000_000), true);
  }
  assert.equal(cache.size, 8);
});

test('expired entries are pruned even below the count bound', () => {
  const cache = new ReplayCache(30);
  cache.checkAndAdd(1n, 1_000);
  assert.equal(cache.size, 1);
  cache.checkAndAdd(2n, 1_000 + 31); // first is now outside the window
  assert.equal(cache.size, 1);
});

/** Send one datagram and resolve; used to drive the live listener. */
function sendTo(port: number, packet: Buffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4');
    socket.send(packet, port, '127.0.0.1', (err) => {
      socket.close();
      if (err) reject(err);
      else resolve();
    });
  });
}

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));

test('listen(): a valid packet fires onSleep, a forged one does not', async () => {
  const key = 'cd'.repeat(32);
  const fired: string[] = [];
  const rejected: string[] = [];

  const listener = await listen({
    key,
    port: 0, // let the OS pick, so concurrent test runs cannot collide
    address: '127.0.0.1',
    onSleep: () => void fired.push('slept'),
    onReject: (r) => void rejected.push(r.reason ?? '?'),
  });

  try {
    await sendTo(listener.port, buildSleepPacket({ host: 'x', key }));
    await settle();
    assert.deepEqual(fired, ['slept']);

    const forged = buildSleepPacket({ host: 'x', key: 'ef'.repeat(32) });
    await sendTo(listener.port, forged);
    await settle();
    assert.deepEqual(fired, ['slept'], 'a forged packet must not suspend anything');
    assert.deepEqual(rejected, ['bad-signature']);

    await sendTo(listener.port, buildMagicPacket('48:b0:2d:11:22:33'));
    await settle();
    assert.deepEqual(fired, ['slept']);
    assert.deepEqual(rejected, ['bad-signature', 'wrong-length']);
  } finally {
    await listener.close();
  }
});

test('listen(): the same packet twice is refused the second time', async () => {
  const key = 'cd'.repeat(32);
  const fired: string[] = [];
  const rejected: string[] = [];
  const listener = await listen({
    key,
    port: 0,
    address: '127.0.0.1',
    onSleep: () => void fired.push('slept'),
    onReject: (r) => void rejected.push(r.reason ?? '?'),
  });

  try {
    const packet = buildSleepPacket({ host: 'x', key });
    await sendTo(listener.port, packet);
    await settle();
    await sendTo(listener.port, packet);
    await settle();
    assert.deepEqual(fired, ['slept'], 'the replay must not suspend a second time');
    assert.deepEqual(rejected, ['replayed-nonce']);
  } finally {
    await listener.close();
  }
});

test('listen(): answers nothing at all, to anyone', async () => {
  // Not a nicety. A UDP service that replies to unverified input is a
  // reflection amplifier, and a reply that differs between "bad signature"
  // and "replayed nonce" tells a prober which one they achieved.
  const key = 'cd'.repeat(32);
  const listener = await listen({
    key,
    port: 0,
    address: '127.0.0.1',
    onSleep: () => {},
    onReject: () => {},
  });

  try {
    const replies: Buffer[] = [];
    const client = createSocket('udp4');
    await new Promise<void>((r) => client.bind(0, '127.0.0.1', () => r()));
    client.on('message', (msg) => void replies.push(msg));

    for (const packet of [
      buildSleepPacket({ host: 'x', key }), // valid
      buildSleepPacket({ host: 'x', key: 'ef'.repeat(32) }), // forged
      Buffer.alloc(0), // empty
    ]) {
      client.send(packet, listener.port, '127.0.0.1');
      await settle(200);
    }

    client.close();
    assert.deepEqual(replies, [], 'the listener must never send anything back');
  } finally {
    await listener.close();
  }
});

test('listen(): a throwing onSleep is logged, not fatal', async () => {
  const key = 'cd'.repeat(32);
  let second = false;
  const listener = await listen({
    key,
    port: 0,
    address: '127.0.0.1',
    onSleep: () => {
      if (second) return;
      second = true;
      throw new Error('boat-sleep exploded');
    },
    onReject: () => {},
  });

  try {
    await sendTo(listener.port, buildSleepPacket({ host: 'x', key }));
    await settle();
    // Still alive and still serving after the handler threw.
    await sendTo(listener.port, buildSleepPacket({ host: 'x', key }));
    await settle();
    assert.equal(second, true);
  } finally {
    await listener.close();
  }
});
