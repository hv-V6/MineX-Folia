#!/usr/bin/env node
/* Stream a remote log and print only lines matching a regex, with a byte cap.
 * Avoids downloading the whole file through the slow SFTP path.
 * usage: node loggrep.js <remote_file> <regex> [maxBytesFromEnd]
 */
const { Client } = require('ssh2');

const CFG = {
  host: process.env.MX_SFTP_HOST || 'node.minex.gg',
  port: Number(process.env.MX_SFTP_PORT || 2022),
  username: process.env.MX_SFTP_USER || 'CHANGEME',
  password: process.env.MX_SFTP_PASS || 'CHANGEME_SFTP_PASSWORD',
  readyTimeout: 30000,
  keepaliveInterval: 10000,
};

const file = process.argv[2];
const re = new RegExp(process.argv[3]);
const maxBytes = Number(process.argv[4] || 6 * 1024 * 1024);

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
        let hits = 0;
        const counts = new Map();
        for (const l of lines) {
          if (!re.test(l)) continue;
          hits++;
          const m = l.match(/lost connection: (.*)$/);
          const key = m ? m[1].trim() : l.replace(/^\[[^\]]*\]\s*/, '').slice(0, 120);
          counts.set(key, (counts.get(key) || 0) + 1);
        }
        console.log(JSON.stringify({ file, scannedBytes: st.size - start, hits }, null, 2));
        [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15)
          .forEach(([k, v]) => console.log(`${String(v).padStart(5)}  ${k}`));
        conn.end();
      });
    });
  });
});
conn.on('error', (e) => { console.error(e.message); process.exit(1); });
conn.connect(CFG);
