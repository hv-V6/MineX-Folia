#!/usr/bin/env node
/* Summarise a Folia/ZGC stall.log: collection frequency, pause lengths, trims.
 * usage: node gcanalyze.js <remote_file> [maxBytesFromEnd]
 */
const { Client } = require('ssh2');

const CFG = {
  host: process.env.MX_SFTP_HOST || 'node.minex.gg',
  port: Number(process.env.MX_SFTP_PORT || 2022),
  username: process.env.MX_SFTP_USER,
  password: process.env.MX_SFTP_PASS || 'CHANGEME_SFTP_PASSWORD',
  readyTimeout: 30000,
  keepaliveInterval: 10000,
};

const file = process.argv[2];
const maxBytes = Number(process.argv[3] || 8 * 1024 * 1024);

const conn = new Client();
conn.on('ready', () => {
  conn.sftp((err, sftp) => {
    if (err) { console.error(err.message); process.exit(1); }
    sftp.stat(file, (e, st) => {
      if (e) { console.error(e.message); process.exit(1); }
      const start = Math.max(0, st.size - maxBytes);
      const chunks = [];
      const s = sftp.createReadStream(file, { start, end: st.size - 1 });
      s.on('data', (c) => chunks.push(c));
      s.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        const lines = text.split('\n');

        const gc = [];        // {kind, before, after, dur}
        const trims = [];     // duration ms
        let safepointMax = 0;

        for (const l of lines) {
          let m = l.match(/GC\(\d+\) (Major|Minor) Collection[^)]*\)[^ ]* ?([\d.]+)?M?\((\d+)%\)->([\d.]+)M\((\d+)%\) ([\d.]+)s/);
          if (!m) m = l.match(/GC\(\d+\) (Major|Minor) Collection.*?([\d.]+)M\((\d+)%\)->([\d.]+)M\((\d+)%\) ([\d.]+)s/);
          if (m) {
            gc.push({
              kind: m[1],
              after: parseFloat(m[4] ?? m[3]),
              dur: parseFloat(m[6] ?? m[5]),
            });
            continue;
          }
          const t = l.match(/Periodic Trim \(\d+\):.*?([\d.]+)ms/);
          if (t) { trims.push(parseFloat(t[1])); continue; }
          const sp = l.match(/Safepoint .*Total: (\d+) ns/);
          if (sp) safepointMax = Math.max(safepointMax, Number(sp[1]) / 1e6);
        }

        const dur = (a) => a.length ? a.reduce((x, y) => x + y, 0) : 0;
        const majors = gc.filter((g) => g.kind === 'Major');
        const minors = gc.filter((g) => g.kind === 'Minor');

        console.log(`file: ${file}`);
        console.log(`window: ${(st.size - start) / 1048576} MB of log\n`);

        console.log(`collections: ${gc.length} (major ${majors.length}, minor ${minors.length})`);
        for (const [name, arr] of [['major', majors], ['minor', minors]]) {
          if (!arr.length) continue;
          const d = arr.map((x) => x.dur);
          console.log(`  ${name}: avg ${(dur(d) / d.length).toFixed(3)}s ` +
            `max ${Math.max(...d).toFixed(3)}s total ${dur(d).toFixed(1)}s`);
        }
        if (gc.length > 1) {
          // rough cadence from majors
          if (majors.length > 1) {
            console.log(`  major cadence: ~1 per ${
              ((lines.length && 0) || 0)} (see spans)`);
          }
        }
        const allDur = gc.map((g) => g.dur);
        console.log(`\ntotal GC pause time: ${dur(allDur).toFixed(1)}s`);

        console.log(`\ntrims: ${trims.length} (stall.log rolls, so count is partial)`);
        if (trims.length) {
          console.log(`  trim avg ${(dur(trims) / trims.length).toFixed(1)}ms ` +
            `max ${Math.max(...trims).toFixed(1)}ms`);
        }
        console.log(`max safepoint time: ${safepointMax.toFixed(3)}ms`);
        conn.end();
      });
    });
  });
});
conn.on('error', (e) => { console.error(e.message); process.exit(1); });
conn.connect(CFG);
