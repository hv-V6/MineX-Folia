#!/usr/bin/env node
/* Pterodactyl console client: sends commands and captures console output.
 * usage: node console.js <command...>   e.g. node console.js "tps server 5"
 * env: MX_PANEL / MX_TOKEN / MX_SERVER
 */
const WebSocket = require('ws');
const https = require('https');

const PANEL = process.env.MX_PANEL || 'panel.minex.gg';
const TOKEN = process.env.MX_TOKEN || 'CHANGEME';
const SERVER = process.env.MX_SERVER || 'fd4afc59';

function apiGet(path) {
  return new Promise((resolve, reject) => {
    const req = https.request({
      host: PANEL, path, method: 'GET',
      headers: { Authorization: `Bearer ${TOKEN}`, Accept: 'application/json' },
    }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => {
        try { resolve(JSON.parse(d)); } catch (e) { reject(new Error(d)); }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

function apiPost(path, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request({
      host: PANEL, path, method: 'POST',
      headers: {
        Authorization: `Bearer ${TOKEN}`, Accept: 'application/json',
        'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload),
      },
    }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => resolve({ status: res.statusCode, body: d }));
    });
    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const cmds = process.argv.slice(2);
  const durationMs = Number(process.env.MX_LISTEN_MS || 12000);
  const wsCred = await apiGet(`/api/client/servers/${SERVER}/websocket`);
  const { token, socket } = wsCred.data;

  const ws = new WebSocket(socket, {
    headers: { Origin: `https://${PANEL}` },
    rejectUnauthorized: false,
  });

  const lines = [];

  ws.on('open', () => {
    ws.send(JSON.stringify({ event: 'auth', args: [token] }));
  });

  ws.on('message', async (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }
    if (msg.event === 'auth success') {
      for (const c of cmds) {
        const r = await apiPost(`/api/client/servers/${SERVER}/command`, { command: c });
        if (r.status >= 400) console.error(`send failed (${r.status}) for ${c}: ${r.body}`);
        await sleep(1200);
      }
    } else if (msg.event === 'console output') {
      let line = msg.args[0] || '';
      line = line.replace(/\x1b\[[0-9;]*m/g, '');
      line = line.replace(/^\[[\d:]+\]\s*/, '');
      lines.push(line);
    }
  });

  ws.on('error', (e) => { console.error('ws error: ' + e.message); });

  await sleep(durationMs);
  try { ws.close(); } catch {}
  console.log(lines.join('\n'));
  process.exit(0);
})().catch((e) => { console.error('ERROR: ' + e.message); process.exit(1); });
