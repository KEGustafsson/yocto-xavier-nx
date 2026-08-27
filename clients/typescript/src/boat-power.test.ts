/**
 * Tests for the wake/sleep client.
 *
 * The interesting ones are not "does buildSleepPacket return 60 bytes" but
 * "would the board accept what this produces". Wire-format bugs between two
 * independent implementations are exactly the kind that pass every unit test
 * on each side and fail on the boat, so the cross-check against the real
 * boat-sleepd.py runs here too - see cross-check.mjs, which drives both.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { createHmac } from 'node:crypto';

import {
  PACKET_LEN,
  buildMagicPacket,
  buildSleepPacket,
  parseMac,
  verifySleepPacket,
} from './boat-power.js';

const KEY = '00'.repeat(16) + 'ff'.repeat(16);

test('parseMac accepts the three spellings and rejects the rest', () => {
  const want = Buffer.from('48b02d112233', 'hex');
  assert.deepEqual(parseMac('48:b0:2d:11:22:33'), want);
  assert.deepEqual(parseMac('48-b0-2d-11-22-33'), want);
  assert.deepEqual(parseMac('48b02d112233'), want);
  for (const bad of ['', '48:b0:2d:11:22', '48:b0:2d:11:22:33:44', 'zz:b0:2d:11:22:33']) {
    assert.throws(() => parseMac(bad), /not a MAC address/, `should reject ${bad}`);
  }
});

test('magic packet is 102 bytes: 6 x 0xFF then the MAC 16 times', () => {
  const packet = buildMagicPacket('48:b0:2d:11:22:33');
  assert.equal(packet.length, 102);
  assert.deepEqual(packet.subarray(0, 6), Buffer.alloc(6, 0xff));
  for (let i = 0; i < 16; i++) {
    assert.deepEqual(
      packet.subarray(6 + i * 6, 12 + i * 6),
      Buffer.from('48b02d112233', 'hex'),
      `repetition ${i}`,
    );
  }
});

test('sleep packet has the documented layout', () => {
  const packet = buildSleepPacket({ host: 'x', key: KEY, timestamp: 1_700_000_000 });
  assert.equal(packet.length, PACKET_LEN);
  assert.equal(packet.subarray(0, 8).toString('ascii'), 'BOATSLP1');
  assert.equal(packet.readUInt8(8), 1, 'version');
  assert.equal(packet.readUInt8(9), 1, 'opcode = sleep');
  assert.equal(packet.readUInt16BE(10), 0, 'reserved');
  assert.equal(packet.readBigUInt64BE(12), 1_700_000_000n, 'timestamp');
  const expected = createHmac('sha256', Buffer.from(KEY, 'hex'))
    .update(packet.subarray(0, 28))
    .digest();
  assert.deepEqual(packet.subarray(28), expected, 'mac covers the whole header');
});

test('the nonce actually varies', () => {
  const nonces = new Set<string>();
  for (let i = 0; i < 64; i++) {
    nonces.add(
      buildSleepPacket({ host: 'x', key: KEY, timestamp: 1 })
        .subarray(20, 28)
        .toString('hex'),
    );
  }
  assert.equal(nonces.size, 64, 'every packet should carry a distinct nonce');
});

test('verify accepts our own packet and rejects tampering', () => {
  const packet = buildSleepPacket({ host: 'x', key: KEY });
  assert.ok(verifySleepPacket(packet, KEY));
  assert.ok(!verifySleepPacket(packet, 'ab'.repeat(32)), 'wrong key');
  assert.ok(!verifySleepPacket(packet.subarray(0, 59), KEY), 'truncated');
  assert.ok(!verifySleepPacket(Buffer.concat([packet, Buffer.of(0)]), KEY), 'extended');
  for (const offset of [0, 8, 12, 20, 27, 28, 59]) {
    const t = Buffer.from(packet);
    t[offset]! ^= 0x01;
    assert.ok(!verifySleepPacket(t, KEY), `flipped byte ${offset} should not verify`);
  }
});

test('a WoL magic packet is not a valid sleep packet', () => {
  // The whole point of the design: the two directions are different things.
  assert.ok(!verifySleepPacket(buildMagicPacket('48:b0:2d:11:22:33'), KEY));
});

test('key parsing rejects what the board would also reject', () => {
  assert.throws(() => buildSleepPacket({ host: 'x', key: 'hunter2' }), /must be hex/);
  assert.throws(() => buildSleepPacket({ host: 'x', key: 'abc' }), /must be hex/);
  assert.throws(() => buildSleepPacket({ host: 'x', key: 'aabbccdd' }), /only 4 bytes/);
});

test('a raw Buffer key gives the same packet as its hex spelling', () => {
  const a = buildSleepPacket({ host: 'x', key: KEY, timestamp: 42 });
  const b = buildSleepPacket({ host: 'x', key: Buffer.from(KEY, 'hex'), timestamp: 42 });
  // Nonces differ by design, so compare everything the nonce does not cover.
  assert.deepEqual(a.subarray(0, 20), b.subarray(0, 20));
  assert.ok(verifySleepPacket(b, KEY));
});
