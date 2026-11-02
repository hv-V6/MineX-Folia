#!/usr/bin/env node
/* Distribution of Periodic Trim stall times from a Folia stall.log.
 * usage: node trimdist.js <remote_file> [maxBytes]
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
const maxBytes = Number(process.argv[3] || 8 * 1024 * 1024);

const conn = new Client();
conn.on('ready', () => {
  conn.sftp((err, sftp) => {
    if (err) { console.error(err.message); process.exit(1); }
    sftp.stat(file, (e, st) => {
      const start = Math.max(0, st.size - maxBytes);
      const chunks = [];
      sftp.createReadStream(file, { start, end: st.size - 1 })
        .on('data', (c) => chunks.push(c))
        .on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          const trims = [];
          const stamps = [];
          for (const l of text.split('\n')) {
            const m = l.match(/Periodic Trim \(\d+\):.*?([\d.]+)ms/);
            if (m) {
              trims.push(parseFloat(m[1]));
              const ts = l.match(/^\[([\d-]+T[\d:.]+)/);
              if (ts) stamps.push(new Date(ts[1]).getTime());
            }
          }
          if (!trims.length) { console.log('no trims found'); conn.end(); return; }
          trims.sort((a, b) => a - b);
          const pct = (p) => trims[Math.min(trims.length - 1,
            Math.floor((p / 100) * trims.length))];
          const sum = trims.reduce((a, b) => a + b, 0);

          console.log(`trims: ${trims.length}`);
          console.log(`  avg ${(sum / trims.length).toFixed(1)}ms  ` +
            `p50 ${pct(50).toFixed(1)}ms  p95 ${pct(95).toFixed(1)}ms  ` +
            `p99 ${pct(99).toFixed(1)}ms  max ${trims[trims.length - 1].toFixed(1)}ms`);
          const over50 = trims.filter((t) => t > 50).length;
          const over100 = trims.filter((t) => t > 100).length;
          const over500 = trims.filter((t) => t > 500).length;
          console.log(`  >50ms: ${over50} (${(100 * over50 / trims.length).toFixed(1)}%)  ` +
            `>100ms: ${over100}  >500ms: ${over500}`);

          if (stamps.length > 1) {
            const span = (stamps[stamps.length - 1] - stamps[0]) / 1000;
            const gaps = [];
            for (let i = 1; i < stamps.length; i++) gaps.push((stamps[i] - stamps[i - 1]) / 1000);
            gaps.sort((a, b) => a - b);
            console.log(`  span ${(span / 3600).toFixed(2)}h, ` +
              `median gap ${gaps[Math.floor(gaps.length / 2)].toFixed(1)}s`);
          }
          conn.end();
        });
    });
  });
});
conn.on('error', (e) => { console.error(e.message); process.exit(1); });
conn.connect(CFG);
