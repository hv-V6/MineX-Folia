#!/usr/bin/env python3
"""A/B load comparison for two Folia builds, driven by server-side flight.

Design constraints learned the hard way:

1. A synthetic client cannot win a fight with movement validation. A position
   packet that is too large, or two inside one tick, gets the client dropped with
   "Invalid move player packet received". So bots never send movement; the server
   drives them with `/tppos`, which produces identical chunk-load/send/entity-
   tracking cost with none of the client-side validation.
2. Bots must be serviced continuously. A console call takes seconds, so if the
   teleport driver shared a thread with the bot readers, keepalives would be
   missed and the whole fleet would be kicked. Every bot runs in its own thread;
   only the driver issues console commands.

The workload is identical for both jars, so the comparison is fair.

usage:
  python abflight.py --host H --port P --secret S --count 30 \
      --label upstream --hold 240 --out ab.jsonl
"""
import argparse
import json
import math
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcjoin import Client, status_protocol   # noqa: E402

SFTP_DIR = r'E:\minex\_tools\sftp'
PANEL = 'panel.minex.gg'
SERVER = 'fd4afc59'
TOKEN = 'CHANGEME_PANEL_TOKEN'

_console_lock = threading.Lock()


def console(*cmds):
    with _console_lock:
        try:
            out = subprocess.run(['node', 'console.js', *cmds], cwd=SFTP_DIR,
                                 capture_output=True, text=True, timeout=240)
            return out.stdout + out.stderr
        except Exception as e:
            return f'[console error] {e}'


def resources():
    req = urllib.request.Request(
        f'https://{PANEL}/api/client/servers/{SERVER}/resources',
        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20,
                                context=ssl.create_default_context()) as r:
        a = json.loads(r.read().decode())['attributes']
    res = a['resources']
    return {'state': a['current_state'],
            'rss_mb': round(res['memory_bytes'] / 1048576, 1),
            'cpu': round(res['cpu_absolute'], 1)}


def health():
    out = console('paper:tps server 5', 'mxsr status')
    h = {'players': None, 'regions': None, 'tps_min': None, 'tps_med': None,
         'mspt_max': None, 'util': None, 'ghosts': None, 'quarantined': None,
         'failures': None, 'chunks': None}
    for l in out.splitlines():
        s = l.strip()
        try:
            if s.startswith('- Online Players:'):
                h['players'] = int(s.split(':')[1])
            elif s.startswith('- Total Regions:') or s.startswith('- Total regions:'):
                h['regions'] = int(s.split(':')[1])
            elif s.startswith('- Lowest Region TPS:'):
                h['tps_min'] = float(s.split(':')[1])
            elif s.startswith('- Median Region TPS:'):
                h['tps_med'] = float(s.split(':')[1])
            elif s.startswith('- Utilisation:'):
                h['util'] = s.split(':')[1].strip()
            elif s.startswith('- Chunks:'):
                h['chunks'] = int(s.split(':')[1].replace(',', ''))
            elif s.startswith('- Ghost joins detected this session:'):
                h['ghosts'] = int(s.split(':')[1])
            elif s.startswith('- Quarantined regions:'):
                h['quarantined'] = int(s.split(':')[1])
            elif s.startswith('- Total region failures this session:'):
                h['failures'] = int(s.split(':')[1])
            elif 'MSPT' in s.upper() and 'util at' in s:
                parts = s.split()
                for i, p in enumerate(parts):
                    if p.upper().endswith('MSPT'):
                        h['mspt_max'] = max(h['mspt_max'] or 0, float(parts[i - 1]))
        except Exception:
            pass
    return h


class Bot(threading.Thread):
    """Holds one connection open and answers keepalives. Never sends movement."""

    daemon = True

    def __init__(self, host, port, name, proto, secret, hold):
        super().__init__(name=name)
        self.host, self.port, self.name = host, port, name
        self.proto, self.secret, self.hold = proto, secret, hold
        self.c = None
        self.joined = False
        self.err = None

    def run(self):
        try:
            self.c = Client(self.host, self.port, self.name, self.proto,
                            quiet=True, secret=self.secret)
            self.c.connect()
            if not self.c.login_until_play(duration=150):
                self.err = 'no-play'
                return
            self.joined = True
            end = time.time() + self.hold
            while time.time() < end:
                span = max(1, int(min(30, end - time.time())))
                if not self.c.stay(span, move=False):
                    self.err = 'dropped'
                    return
        except Exception as e:
            self.err = f'{type(e).__name__}: {e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--secret', default=None)
    ap.add_argument('--count', type=int, default=30)
    ap.add_argument('--hold', type=int, default=240)
    ap.add_argument('--label', required=True)
    ap.add_argument('--radius', type=int, default=260)
    ap.add_argument('--cell', type=int, default=900)
    ap.add_argument('--sample', type=int, default=15)
    ap.add_argument('--out', default='ab.jsonl')
    a = ap.parse_args()

    proto = status_protocol(a.host, a.port)['version']['protocol']
    print(f'[i] {a.label}: {a.count} bots, server-driven flight, hold {a.hold}s',
          flush=True)

    bots = [Bot(a.host, a.port, f'{a.label}{i:03d}', proto, a.secret,
                a.hold + 300) for i in range(a.count)]
    for b in bots:
        b.start()
        time.sleep(0.22)

    t0 = time.time()
    while time.time() - t0 < 200:
        if sum(1 for b in bots if b.joined) >= a.count:
            break
        time.sleep(2)
    live = [b for b in bots if b.joined]
    print(f'[+] joined {len(live)}/{a.count}', flush=True)
    if not live:
        return 1

    cmds = []
    for b in live:
        cmds += [f'gamemode creative {b.name}', f'flyspeed {b.name} 2']
    for i in range(0, len(cmds), 24):
        console(*cmds[i:i + 24])
    print('[i] creative + flyspeed 2 set', flush=True)

    recs = []
    step = 0
    start = time.time()
    next_sample = start
    out_f = open(a.out, 'a', encoding='utf-8')

    while time.time() - start < a.hold:
        step += 1
        cmds = []
        for i, b in enumerate(live):
            ang = step * 0.3 + i * 2.399     # golden angle spreads the fleet
            x = int(math.cos(ang) * a.radius) + (i % 4) * a.cell
            z = int(math.sin(ang) * a.radius) + (i // 4) * a.cell
            cmds.append(f'tppos -p:{b.name} {x} 150 {z}')
        for i in range(0, len(cmds), 20):
            console(*cmds[i:i + 20])

        if time.time() >= next_sample:
            next_sample = time.time() + a.sample
            hh = health()
            try:
                rs = resources()
            except Exception:
                rs = {}
            rec = {'label': a.label, 't': round(time.time() - start, 1),
                   'step': step, **hh, **rs}
            recs.append(rec)
            out_f.write(json.dumps(rec) + '\n')
            out_f.flush()
            print(f"    {a.label} t={rec['t']:.0f}s players={hh['players']} "
                  f"regions={hh['regions']} tps={hh['tps_min']} "
                  f"mspt={hh['mspt_max']} chunks={hh['chunks']} "
                  f"rss={rs.get('rss_mb')}MB cpu={rs.get('cpu')}%", flush=True)

    out_f.close()

    tl = [r['tps_min'] for r in recs if r.get('tps_min') is not None]
    ml = [r['mspt_max'] for r in recs if r.get('mspt_max') is not None]
    rl = [r['rss_mb'] for r in recs if r.get('rss_mb') is not None]
    cl = [r['cpu'] for r in recs if r.get('cpu') is not None]
    gl = [r['regions'] for r in recs if r.get('regions') is not None]
    print(f'\n[=] {a.label} join={len(live)}/{a.count} flight_steps={step}', flush=True)
    if gl:
        print(f'[=] regions max={max(gl)}', flush=True)
    if tl:
        print(f'[=] tps min={min(tl):.2f} avg={sum(tl)/len(tl):.2f}', flush=True)
    if ml:
        print(f'[=] mspt max={max(ml):.2f} avg={sum(ml)/len(ml):.2f}', flush=True)
    if rl:
        print(f'[=] rss max={max(rl):.0f} avg={sum(rl)/len(rl):.0f}', flush=True)
    if cl:
        print(f'[=] cpu avg={sum(cl)/len(cl):.1f} max={max(cl):.1f}', flush=True)

    for b in bots:
        try:
            if b.c:
                b.c.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
