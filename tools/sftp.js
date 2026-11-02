#!/usr/bin/env node
/* MineX SFTP helper (dev access)
 * env: MX_SFTP_USER / MX_SFTP_PASS / MX_SFTP_HOST / MX_SFTP_PORT
 * usage:
 *   node sftp.js ls <remote_dir>
 *   node sftp.js stat <remote_file>
 *   node sftp.js cat <remote_file> [maxBytes] [offset]
 *   node sftp.js tail <remote_file> [bytes]
 *   node sftp.js get <remote_file> <local_file>
 *   node sftp.js put <local_file> <remote_file>
 *   node sftp.js mkdir <remote_dir>
 *   node sftp.js rm <remote_file>
 *   node sftp.js mv <from> <to>
 *   node sftp.js du <remote_dir>
 *   node sftp.js find <remote_dir> <regex>
 *   node sftp.js grep <remote_dir> <regex> [maxFileBytes] [fileGlobRegex]
 */
const { Client } = require('ssh2');
const fs = require('fs');

const CFG = {
  host: process.env.MX_SFTP_HOST || 'node.minex.gg',
  port: Number(process.env.MX_SFTP_PORT || 2022),
  username: process.env.MX_SFTP_USER || 'CHANGEME',
  password: process.env.MX_SFTP_PASS || 'CHANGEME_SFTP_PASSWORD',
  readyTimeout: 30000,
  keepaliveInterval: 10000,
};

function connect() {
  return new Promise((resolve, reject) => {
    const c = new Client();
    c.on('ready', () => c.sftp((err, sftp) => (err ? reject(err) : resolve({ c, sftp }))));
    c.on('error', reject);
    c.connect(CFG);
  });
}

const prom = (fn, ...a) => new Promise((res, rej) => fn(...a, (e, r) => (e ? rej(e) : res(r))));

function readRange(sftp, remote, start, len) {
  return new Promise((resolve, reject) => {
    if (len <= 0) return resolve(Buffer.alloc(0));
    const chunks = [];
    const stream = sftp.createReadStream(remote, { start, end: start + len - 1 });
    stream.on('data', (d) => chunks.push(d));
    stream.on('error', reject);
    stream.on('end', () => resolve(Buffer.concat(chunks)));
  });
}

async function walk(sftp, dir, out, depth = 0, maxDepth = 12) {
  if (depth > maxDepth) return;
  let list;
  try { list = await prom(sftp.readdir.bind(sftp), dir); } catch { return; }
  for (const e of list) {
    const p = dir.replace(/\/$/, '') + '/' + e.filename;
    out.push({ p, size: e.attrs.size, dir: e.attrs.isDirectory() });
    if (e.attrs.isDirectory() && e.filename !== '.' && e.filename !== '..') {
      await walk(sftp, p, out, depth + 1, maxDepth);
    }
  }
}

(async () => {
  const [op, a1, a2, a3, a4] = process.argv.slice(2);
  const { c, sftp } = await connect();
  try {
    switch (op) {
      case 'ls': {
        const dir = a1 || '/';
        const list = await prom(sftp.readdir.bind(sftp), dir);
        list.sort((x, y) => (y.attrs.isDirectory() - x.attrs.isDirectory()) || x.filename.localeCompare(y.filename));
        for (const e of list) {
          console.log(`${e.attrs.isDirectory() ? 'd' : '-'} ${String(e.attrs.size).padStart(12)} ${new Date(e.attrs.mtime * 1000).toISOString()} ${e.filename}`);
        }
        break;
      }
      case 'stat': {
        console.log(JSON.stringify(await prom(sftp.stat.bind(sftp), a1), null, 2));
        break;
      }
      case 'cat': {
        const max = Number(a2 || 400000);
        const off = Number(a3 || 0);
        const buf = await readRange(sftp, a1, off, max);
        process.stdout.write(buf);
        break;
      }
      case 'tail': {
        const want = Number(a2 || 200000);
        const st = await prom(sftp.stat.bind(sftp), a1);
        const start = Math.max(0, st.size - want);
        const buf = await readRange(sftp, a1, start, st.size - start);
        process.stdout.write(buf);
        break;
      }
      case 'get': {
        await prom(sftp.fastGet.bind(sftp), a1, a2);
        console.log('got ' + a1 + ' -> ' + a2);
        break;
      }
      case 'put': {
        await prom(sftp.fastPut.bind(sftp), a1, a2);
        console.log('put ' + a1 + ' -> ' + a2);
        break;
      }
      case 'mkdir': {
        await prom(sftp.mkdir.bind(sftp), a1).catch(() => {});
        console.log('mkdir ' + a1);
        break;
      }
      case 'rm': {
        const st = await prom(sftp.stat.bind(sftp), a1).catch(() => null);
        if (st && st.isDirectory()) await prom(sftp.rmdir.bind(sftp), a1, true);
        else await prom(sftp.unlink.bind(sftp), a1);
        console.log('rm ' + a1);
        break;
      }
      case 'mv': {
        await prom(sftp.rename.bind(sftp), a1, a2);
        console.log(`mv ${a1} -> ${a2}`);
        break;
      }
      case 'du': {
        const out = [];
        await walk(sftp, a1 || '/', out);
        let tot = 0, files = 0;
        const buckets = {};
        for (const f of out) {
          if (f.dir) continue;
          tot += f.size; files++;
          const parts = f.p.split('/').filter(Boolean);
          const key = parts.slice(0, 2).join('/');
          buckets[key] = (buckets[key] || 0) + f.size;
        }
        console.log('total bytes: ' + tot + ' (' + (tot / 1048576).toFixed(1) + ' MiB) files: ' + files);
        Object.entries(buckets).sort((x, y) => y[1] - x[1]).slice(0, 40)
          .forEach(([k, v]) => console.log(String(v).padStart(14) + '  ' + k));
        break;
      }
      case 'find': {
        const out = [];
        await walk(sftp, a1 || '/', out);
        const rx = new RegExp(a2, 'i');
        out.filter((f) => rx.test(f.p)).slice(0, 2000).forEach((f) => console.log((f.dir ? 'd' : '-') + ' ' + String(f.size).padStart(12) + ' ' + f.p));
        break;
      }
      case 'grep': {
        const out = [];
        await walk(sftp, a1 || '/', out);
        const rx = new RegExp(a2, 'i');
        const maxb = Number(a3 || 400000);
        const glob = a4 ? new RegExp(a4, 'i') : /\.(log|txt|yml|yaml|json|conf|properties|sh|md|toml|ini)$/i;
        for (const f of out) {
          if (f.dir || f.size > maxb || f.size === 0) continue;
          if (!glob.test(f.p)) continue;
          let data;
          try { data = await readRange(sftp, f.p, 0, f.size); } catch { continue; }
          data.toString('utf8').split('\n').forEach((line, i) => {
            if (rx.test(line)) console.log(`${f.p}:${i + 1}: ${line.slice(0, 400)}`);
          });
        }
        break;
      }
      default:
        console.error('unknown op ' + op);
        process.exitCode = 2;
    }
  } finally {
    c.end();
  }
})().catch((e) => {
  console.error('ERROR: ' + (e && e.message ? e.message : e));
  process.exit(1);
});
