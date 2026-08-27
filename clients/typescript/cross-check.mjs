/**
 * Cross-check this client against the REAL target-side daemon.
 *
 *   npm run build && node cross-check.mjs
 *
 * The unit tests prove this client agrees with itself. They cannot catch the
 * failure that actually matters - a wire-format disagreement between the
 * TypeScript sender and the Python receiver - because both sides would pass
 * their own tests and the mismatch would only appear on a boat, as a sleep
 * command that silently does nothing. So this starts
 * layers/meta-boat/recipes-boat/power/files/boat-sleepd.py for real, on a
 * loopback port, and checks what it says about packets this client builds.
 *
 * Requires python3, which the build host already has.
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomBytes } from 'node:crypto';

import { buildMagicPacket, buildSleepPacket, sleep } from './dist/boat-power.js';

const DAEMON = resolve(
  import.meta.dirname,
  '../../layers/meta-boat/recipes-boat/power/files/boat-sleepd.py',
);
const PORT = 19095;
const HOST = '127.0.0.1';

const dir = mkdtempSync(join(tmpdir(), 'boat-xcheck-'));
const keyFile = join(dir, 'key');
const key = randomBytes(32).toString('hex');
writeFileSync(keyFile, key + '\n', { mode: 0o600 });

const lines = [];
const daemon = spawn(
  'python3',
  [DAEMON, '--key-file', keyFile, '--port', String(PORT), '--bind', HOST, '--dry-run'],
  { stdio: ['ignore', 'ignore', 'pipe'] },
);
daemon.stderr.setEncoding('utf8');
daemon.stderr.on('data', (chunk) => {
  for (const line of chunk.split('\n')) if (line.trim()) lines.push(line.trim());
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const since = (mark) => lines.slice(mark);

let failures = 0;
function check(name, ok, detail = '') {
  if (ok) {
    console.log(`  PASS  ${name}`);
  } else {
    failures++;
    console.log(`  FAIL  ${name}${detail ? ` -- ${detail}` : ''}`);
  }
}

try {
  await wait(1500);
  if (!lines.some((l) => l.includes('ready'))) {
    throw new Error(`daemon did not start:\n${lines.join('\n')}`);
  }

  // 1. The packet this client builds must be accepted by the real receiver.
  let mark = lines.length;
  await sleep({ host: HOST, port: PORT, key });
  await wait(600);
  check(
    'python daemon authenticates a TypeScript-built packet',
    since(mark).some((l) => l.includes('authenticated sleep request')),
    since(mark).join(' | '),
  );

  // 2. The same packet twice must be caught by the nonce cache, which proves
  //    this client's nonce reaches the field the daemon reads.
  mark = lines.length;
  const once = buildSleepPacket({ host: HOST, key });
  const { createSocket } = await import('node:dgram');
  const sendRaw = (buf) =>
    new Promise((res, rej) => {
      const s = createSocket('udp4');
      s.send(buf, PORT, HOST, (e) => {
        s.close();
        e ? rej(e) : res();
      });
    });
  await sendRaw(once);
  await wait(400);
  await sendRaw(once);
  await wait(600);
  check(
    'replaying one TypeScript packet is rejected as a replayed nonce',
    since(mark).some((l) => l.includes('replayed nonce')),
    since(mark).join(' | '),
  );

  // 3. Wrong key must fail the signature check, not something incidental like
  //    a length or magic mismatch - that would mean the MAC is not actually
  //    being compared.
  mark = lines.length;
  await sendRaw(buildSleepPacket({ host: HOST, key: 'ab'.repeat(32) }));
  await wait(600);
  check(
    'a packet signed with the wrong key is rejected as a bad signature',
    since(mark).some((l) => l.includes('bad signature')),
    since(mark).join(' | '),
  );

  // 4. Clock skew beyond the window must be refused, and the daemon must read
  //    our timestamp from where we wrote it.
  mark = lines.length;
  await sendRaw(
    buildSleepPacket({ host: HOST, key, timestamp: Math.floor(Date.now() / 1000) - 3600 }),
  );
  await wait(600);
  check(
    'an hour-old TypeScript packet is rejected on its timestamp',
    since(mark).some((l) => l.includes('timestamp') && l.includes('behind')),
    since(mark).join(' | '),
  );

  // 5. And the thing this whole design exists to prevent: a magic packet, the
  //    one anybody can forge, must not put the boat to sleep.
  mark = lines.length;
  await sendRaw(buildMagicPacket('48:b0:2d:11:22:33'));
  await wait(600);
  const seen = since(mark);
  check(
    'a Wake-on-LAN magic packet does NOT trigger a suspend',
    seen.some((l) => l.includes('rejected')) &&
      !seen.some((l) => l.includes('authenticated')),
    seen.join(' | '),
  );
} finally {
  daemon.kill();
  rmSync(dir, { recursive: true, force: true });
}

console.log(failures ? `\n${failures} check(s) failed` : '\nall cross-checks passed');
process.exit(failures ? 1 : 0);
