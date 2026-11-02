#!/usr/bin/env node
/* Byte-exact remote text check: counts CR, reports first CR offset, and
 * optionally prints lines matching a regex. Avoids PowerShell re-encoding.
 * usage: node crlf.js <remote_file> [matchRegex]
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
const match = process.argv[3] ? new RegExp(process.argv[3]) : null;
if (!file) {
  console.error('usage: node crlf.js <remote_file> [matchRegex]');
  process.exit(2);
}

const conn = new Client();
conn.on('ready', () => {
  conn.sftp((err, sftp) => {
    if (err) { console.error('sftp: ' + err.message); process.exit(1); }
    const chunks = [];
    const s = sftp.createReadStream(file);
    s.on('data', (c) => chunks.push(c));
    s.on('error', (e) => { console.error('read: ' + e.message); process.exit(1); });
    s.on('end', () => {
      const buf = Buffer.concat(chunks);
      let cr = 0;
      let firstCr = -1;
      for (let i = 0; i < buf.length; i++) {
        if (buf[i] === 0x0d) {
          cr++;
          if (firstCr < 0) firstCr = i;
        }
      }
      // BOM check
      const bom = buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf;
      console.log(JSON.stringify({
        file,
        bytes: buf.length,
        crBytes: cr,
        firstCrOffset: firstCr,
        utf8Bom: bom,
        lfOnly: cr === 0,
      }, null, 2));

      if (match) {
        const text = buf.toString('utf8');
        const lines = text.split('\n');
        const hits = [];
        lines.forEach((l, i) => {
          if (match.test(l)) hits.push((i + 1) + ': ' + l.replace(/\r$/, ''));
        });
        console.log('--- matches (' + hits.length + ') ---');
        hits.forEach((h) => console.log(h));
      }
      conn.end();
    });
  });
});
conn.on('error', (e) => { console.error('conn: ' + e.message); process.exit(1); });
conn.connect(CFG);
