#!/usr/bin/env node
/* Audit a remote log for instability signals and classify them.
 *
 * Groups every ERROR/WARN and exception by a normalised signature, so 5,000
 * lines become a handful of distinct defects with counts. Also flags the
 * Folia-specific failure modes that matter for stability: region tick failures,
 * watchdog stalls, cross-thread violations, chunk-system errors, OOM, crashes.
 *
 * usage: node audit.js <remote_file> [maxBytes]
 */
const { Client } = require('ssh2');

const CFG = {
  host: process.env.MX_SFTP_HOST || 'node.minex.gg',
  port: Number(process.env.MX_SFTP_PORT || 2022),
  username: process.env.MX_SFTP_USER,
  password: process.env.MX_SFTP_PASS || 'CHANGEME_SFTP_PASSWORD',
  readyTimeout: 30000,
};

const file = process.argv[2];
const maxBytes = Number(process.argv[3] || 12 * 1024 * 1024);

const SIGNALS = [
  ['region-tick-failure', /(?:\bRegion\b.*failed|tickRegion.*(?:exception|error)|Region .* has failed)/i],
  ['watchdog-stall', /(?:Global region has not responded|Watchdog thread.*stall|Single tick took)/i],
  ['cross-thread', /Thread failed main thread check/i],
  ['chunk-system', /(?:ChunkHolderManager|moonrise|ChunkSystem).*(?:exception|error|fail)/i],
  ['out-of-memory', /(?:OutOfMemoryError|Java heap space|Direct buffer memory)/i],
  ['native-crash', /(?:SIGSEGV|EXCEPTION_ACCESS_VIOLATION|fatal error|hs_err_pid)/i],
  ['server-crash', /(?:Encountered an unexpected exception|crash-report|Exception ticking)/i],
  ['plugin-disable', /(?:Could not load plugin|Failed to enable|has been disabled by)/i],
  ['network-reset', /(?:Connection reset by peer|read timed out|Broken pipe)/i],
  ['keepalive-kick', /was kicked due to keepalive timeout/i],
];

function norm(line) {
  let s = line.replace(/^\[[^\]]*\]\s*/, '');
  s = s.replace(/\[[^\]]*\/[A-Z]+\]:\s*/, '');
  s = s.replace(/\b\d{1,3}(?:\.\d+)?(?:,\d{3})*\b/g, 'N');
  s = s.replace(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/gi, '<uuid>');
  s = s.replace(/\b[A-Za-z0-9_]{3,16}\[/g, '<player>[');
  s = s.replace(/\([^)]*:\d+\)/g, '(<addr>)');
  s = s.replace(/worlds:[a-z_]+|minecraft:[a-z_]+/g, '<world>');
  s = s.replace(/\s+/g, ' ').trim();
  return s.slice(0, 130);
}

const conn = new Client();
conn.on('ready', () => {
  conn.sftp((err, sftp) => {
    if (err) { console.error(err.message); process.exit(1); }
    sftp.stat(file, (e, st) => {
      if (e) { console.error(e.message); process.exit(1); }
      const start = Math.max(0, st.size - maxBytes);
      const chunks = [];
      sftp.createReadStream(file, { start, end: st.size - 1 })
        .on('data', (c) => chunks.push(c))
        .on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          const lines = text.split('\n');

          const sig = new Map();      // normalised -> count
          const signals = new Map();  // signal -> count
          const examples = new Map();
          for (const l of lines) {
            for (const [name, re] of SIGNALS) {
              if (re.test(l)) {
                signals.set(name, (signals.get(name) || 0) + 1);
                if (!examples.has(name)) examples.set(name, l.trim().slice(0, 200));
              }
            }
            if (/\b(?:ERROR|SEVERE)\b|Exception|Error:/.test(l)) {
              const k = norm(l);
              if (k.length > 12) {
                sig.set(k, (sig.get(k) || 0) + 1);
                if (!examples.has(k)) examples.set(k, l.trim().slice(0, 200));
              }
            }
          }

          console.log(`file: ${file}  window: ${((st.size - start) / 1048576).toFixed(1)} MB`);
          console.log(`lines: ${lines.length}\n`);

          console.log('--- Folia stability signals ---');
          if (!signals.size) console.log('  none found');
          [...signals.entries()].sort((a, b) => b[1] - a[1])
            .forEach(([k, v]) => console.log(`  ${String(v).padStart(6)}  ${k}`));

          console.log('\n--- top distinct errors (normalised) ---');
          [...sig.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15)
            .forEach(([k, v]) => console.log(`  ${String(v).padStart(6)}  ${k}`));

          conn.end();
        });
    });
  });
});
conn.on('error', (e) => { console.error(e.message); process.exit(1); });
conn.connect(CFG);
