#!/usr/bin/env python3
"""Fill the server with spectator bots and find the limits.

Bots join through the real Velocity forwarding path, are switched to spectator
(so no physics/anticheat movement validation and no floating kick), spread out,
and left to fly. After each stage the server's own health report is sampled.

Spectator mode is the right tool here: spectators still receive chunks, so they
generate genuine chunk-send + entity-tracking load, but they cannot fall, die or
trip movement checks.

usage:
  python mcfill.py --host H --port P --secret S --stages 10,20,40,60,100
"""
import argparse
import json
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


def console(*cmds):
    try:
        out = subprocess.run(['node', 'console.js', *cmds], cwd=SFTP_DIR,
                             capture_output=True, text=True, timeout=180)
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
            'cpu': round(res['cpu_absolute'], 1),
            'uptime': res['uptime']}


def health():
    """Parse the Folia health report + mxsr status."""
    out = console('paper:tps server 5', 'mxsr status')
    h = {'players': None, 'regions': None, 'util': None,
         'tps_min': None, 'tps_med': None, 'mspt_max': None,
         'ghosts': None, 'quarantined': None, 'failures': None,
         'chunks': None, 'top': []}
    for l in out.splitlines():
        s = l.strip()
        try:
            if s.startswith('- Online Players:'):
                h['players'] = int(s.split(':')[1])
            elif s.startswith('- Total regions:'):
                h['regions'] = int(s.split(':')[1])
            elif s.startswith('- Utilisation:'):
                h['util'] = s.split(':')[1].strip()
            elif s.startswith('- Lowest Region TPS:'):
                h['tps_min'] = float(s.split(':')[1])
            elif s.startswith('- Median Region TPS:'):
                h['tps_med'] = float(s.split(':')[1])
            elif s.startswith('- Quarantined regions:'):
                h['quarantined'] = int(s.split(':')[1])
            elif s.startswith('- Total region failures this session:'):
                h['failures'] = int(s.split(':')[1])
            elif s.startswith('- Ghost joins detected this session:'):
                h['ghosts'] = int(s.split(':')[1])
            elif s.startswith('- Chunks:'):
                h['chunks'] = int(s.split(':')[1])
            elif 'MSPT' in s.upper() and 'util at' in s:
                parts = s.split()
                for i, p in enumerate(parts):
                    if p.endswith('MSPT'):
                        v = float(parts[i - 1])
                        h['mspt_max'] = max(h['mspt_max'] or 0, v)
        except Exception:
            pass
    return h


class FillBot(threading.Thread):
    daemon = True

    def __init__(self, host, port, name, protocol, secret, hold):
        super().__init__(name=name)
        self.host, self.port, self.name = host, port, name
        self.protocol, self.secret, self.hold = protocol, secret, hold
        self.c = None
        self.joined = False
        self.err = None

    def run(self):
        try:
            self.c = Client(self.host, self.port, self.name, self.protocol,
                            quiet=True, secret=self.secret)
            self.c.connect()
            if not self.c.login_until_play(duration=120):
                self.err = 'no-play'
                return
            self.joined = True
            end = time.time() + self.hold
            while time.time() < end:
                span = max(1, int(min(20, end - time.time())))
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
    ap.add_argument('--stages', default='10,20,40,60,100')
    ap.add_argument('--hold', type=int, default=900)
    ap.add_argument('--spacing', type=int, default=700)
    ap.add_argument('--settle', type=int, default=15)
    ap.add_argument('--sample', type=int, default=40)
    ap.add_argument('--prefix', default='FillBot')
    ap.add_argument('--out', default='fill.jsonl')
    a = ap.parse_args()

    stages = [int(x) for x in a.stages.split(',')]
    proto = status_protocol(a.host, a.port)['version']['protocol']
    print(f'[i] protocol={proto} stages={stages} spacing={a.spacing}', flush=True)

    bots = []
    results = []

    with open(a.out, 'w', encoding='utf-8') as f:
        for target in stages:
            h = health()
            if h['players'] is not None and h['players'] >= target:
                print(f'[i] already at {h["players"]} >= {target}', flush=True)
            need = target - len(bots)
            if need <= 0:
                continue
            print(f'\n[>] === stage {target} (adding {need}) ===', flush=True)

            new = []
            for i in range(len(bots) + 1, target + 1):
                b = FillBot(a.host, a.port, f'{a.prefix}{i:03d}', proto,
                            a.secret, a.hold)
                b.start()
                new.append(b)
                bots.append(b)
                time.sleep(0.30)

            # wait for joins
            t0 = time.time()
            while time.time() - t0 < 150:
                j = sum(1 for b in new if b.joined)
                if j >= len(new):
                    break
                time.sleep(2)
            joined = sum(1 for b in new if b.joined)
            print(f'[+] joined {joined}/{len(new)}', flush=True)

            # spectator + spread
            cmds = []
            for b in new:
                if b.joined:
                    cmds.append(f'gamemode spectator {b.name}')
            for idx, b in enumerate(new):
                if not b.joined:
                    continue
                n = int(b.name.replace(a.prefix, ''))
                gx, gz = n % 8, n // 8
                x, z = gx * a.spacing, gz * a.spacing
                cmds.append(f'tppos -p:{b.name} {x} 120 {z}')
            # send in chunks to keep each console call short
            for i in range(0, len(cmds), 20):
                console(*cmds[i:i + 20])
                time.sleep(0.4)

            time.sleep(a.settle)

            # sample
            rs = None
            t1 = time.time()
            while time.time() - t1 < a.sample:
                h = health()
                try:
                    rs = resources()
                except Exception:
                    rs = None
                rec = {'stage': target, **h, **(rs or {})}
                results.append(rec)
                f.write(json.dumps(rec) + '\n')
                f.flush()
                print(f"    players={h['players']} regions={h['regions']} "
                      f"tps_min={h['tps_min']} mspt={h['mspt_max']} "
                      f"chunks={h['chunks']} rss={rec.get('rss_mb')}MB "
                      f"cpu={rec.get('cpu')}% ghosts={h['ghosts']}", flush=True)
                time.sleep(8)

            # stop conditions
            if rs and rs.get('state') != 'running':
                print(f'[!] server state={rs.get("state")} - stopping', flush=True)
                break
            if h['players'] is not None and h['players'] < target * 0.7:
                print(f'[!] only {h["players"]}/{target} held - cap or drops', flush=True)
                break
            if h['tps_min'] is not None and h['tps_min'] < 15.0:
                print(f'[!] TPS fell to {h["tps_min"]} - limit reached', flush=True)
                break

    print('\n[=] ==== LIMITS ====')
    for r in results:
        print(f"stage={r['stage']:>4} players={r['players']} "
              f"regions={r['regions']} tps={r['tps_min']} "
              f"mspt={r['mspt_max']} rss={r.get('rss_mb')}MB cpu={r.get('cpu')}%")
    return 0


if __name__ == '__main__':
    sys.exit(main())
