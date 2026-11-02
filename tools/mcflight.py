#!/usr/bin/env python3
"""Compare Folia builds under an identical creative-flight workload.

Bots join via Velocity forwarding, are put in creative with a flight-speed
modifier, and then continuously fly a deterministic pattern. The pattern is
seeded per bot so both server builds receive byte-identical movement.

Why this differs from the earlier spectator runs: a creative flying player
forces the server to validate movement and to send/receive chunk data along the
whole flight path, so it exercises far more of the region scheduler than a
static spectator. Movement is derived from a fixed formula, not randomness, so
runs are reproducible and comparable.

usage:
  python mcflight.py --host H --port P --secret S --count 40 --hold 180 \
      --label upstream --out results.jsonl
"""
import argparse
import json
import math
import os
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mcjoin import Client, status_protocol, wv   # noqa: E402

SFTP_DIR = r'E:\minex\_tools\sftp'
PANEL = 'panel.minex.gg'
SERVER = 'fd4afc59'
TOKEN = 'CHANGEME_PANEL_TOKEN'

CB_KEEPALIVE = 0x2d
CB_PING = 0x3e
CB_PLAYER_POSITION = 0x49
SB_KEEPALIVE = 0x1c
SB_MOVE_POS = 0x1e
SB_MOVE_POS_ROT = 0x1f
SB_PONG = 0x2d
SB_ACCEPT_TELEPORT = 0x00
SB_CLIENT_INFO = 0x0e


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
            'cpu': round(res['cpu_absolute'], 1)}


def health():
    out = console('paper:tps server 5', 'mxsr status')
    h = {'players': None, 'regions': None, 'tps_min': None, 'tps_med': None,
         'mspt_max': None, 'util': None, 'ghosts': None, 'quarantined': None,
         'failures': None}
    for l in out.splitlines():
        s = l.strip()
        try:
            if s.startswith('- Online Players:'):
                h['players'] = int(s.split(':')[1])
            elif s.startswith('- Total regions:'):
                h['regions'] = int(s.split(':')[1])
            elif s.startswith('- Lowest Region TPS:'):
                h['tps_min'] = float(s.split(':')[1])
            elif s.startswith('- Median Region TPS:'):
                h['tps_med'] = float(s.split(':')[1])
            elif s.startswith('- Utilisation:'):
                h['util'] = s.split(':')[1].strip()
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


class FlyBot(threading.Thread):
    """Creative bot flying a deterministic Lissajous path."""

    daemon = True

    def __init__(self, host, port, name, protocol, secret, seed, hold,
                 speed=2.0, origin=(0, 140, 0), radius=280):
        super().__init__(name=name)
        self.host, self.port, self.name = host, port, name
        self.protocol, self.secret, self.seed = protocol, secret, seed
        self.hold = hold
        self.speed = speed
        self.ox, self.oy, self.oz = origin
        self.radius = radius
        self.c = None
        self.joined = False
        self.err = None
        self.frames = 0

    def run(self):
        try:
            self.c = Client(self.host, self.port, self.name, self.protocol,
                            quiet=True, secret=self.secret)
            self.c.connect()
            if not self.c.login_until_play(duration=120):
                self.err = 'no-play'
                return
            self.joined = True
            self.fly()
        except Exception as e:
            self.err = f'{type(e).__name__}: {e}'

    def fly(self):
        c = self.c
        end = time.time() + self.hold
        t = self.seed * 0.7
        last_send = 0.0
        interval = 0.12
        # The server teleports us to world spawn on join. Until that arrives we do
        # not know where we are, and a move packet computed from a guessed origin
        # is a multi-hundred-block jump, which the server rejects outright. So wait
        # for the first position packet and anchor the trajectory to it.
        anchor = None
        while time.time() < end and anchor is None:
            try:
                c.sock.settimeout(0.05)
                pid, b = c.read_packet()
                if pid == CB_KEEPALIVE:
                    c.send(SB_KEEPALIVE, b.read(8))
                    c.keepalives += 1
                elif pid == CB_PLAYER_POSITION:
                    tid = b.vi()
                    px, py, pz = struct.unpack('>ddd', b.read(24))
                    b.read(24)
                    yaw, pitch = struct.unpack('>ff', b.read(8))
                    b.read(4)
                    c.send(SB_ACCEPT_TELEPORT, wv(tid) + struct.pack('>ddd', px, py, pz)
                           + struct.pack('>ff', yaw, pitch))
                    c.teleports += 1
                    anchor = (px, py, pz)
            except Exception:
                pass
        if anchor is None:
            self.err = 'no-anchor'
            return
        self.anchor = anchor
        cx, cy, cz = anchor
        # Re-centre the flight box on the anchor so the pendulum stays in loaded
        # chunks and never leaves the region it was assigned.
        radius = min(self.radius, 96.0)

        while time.time() < end:
            try:
                c.sock.settimeout(0.01)
                try:
                    pid, b = c.read_packet()
                    if pid == CB_KEEPALIVE:
                        c.send(SB_KEEPALIVE, b.read(8))
                        c.keepalives += 1
                    elif pid == CB_PING:
                        c.send(SB_PONG, b.read(4))
                    elif pid == CB_PLAYER_POSITION:
                        tid = b.vi()
                        px, py, pz = struct.unpack('>ddd', b.read(24))
                        b.read(24)
                        yaw, pitch = struct.unpack('>ff', b.read(8))
                        b.read(4)
                        c.send(SB_ACCEPT_TELEPORT, wv(tid) + struct.pack('>ddd', px, py, pz)
                               + struct.pack('>ff', yaw, pitch))
                        c.teleports += 1
                except Exception:
                    pass

                now = time.time()
                if now - last_send >= interval:
                    last_send = now
                    t += 0.05
                    # Deterministic trajectory anchored on where the server put us,
                    # kept small enough that every step is a legal incremental move.
                    x = cx + math.sin(t * 0.31) * radius
                    z = cz + math.cos(t * 0.23) * radius
                    y = cy + math.sin(t * 0.17) * 6.0
                    yaw = (math.degrees(t * 0.31) + 90.0) % 360.0
                    pitch = 0.0
                    # flags 0x02 = not on ground (flying)
                    c.send(SB_MOVE_POS_ROT,
                           struct.pack('>dddff', x, y, z, yaw, pitch) + b'\x02')
                    self.frames += 1
            except Exception as e:
                self.err = f'fly: {type(e).__name__}'
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--secret', default=None)
    ap.add_argument('--count', type=int, default=40)
    ap.add_argument('--hold', type=int, default=180)
    ap.add_argument('--label', required=True)
    ap.add_argument('--spacing', type=int, default=560)
    ap.add_argument('--sample', type=int, default=12)
    ap.add_argument('--out', default='flight.jsonl')
    a = ap.parse_args()

    proto = status_protocol(a.host, a.port)['version']['protocol']
    print(f'[i] {a.label}: {a.count} creative flying bots, hold {a.hold}s', flush=True)

    bots = []
    for i in range(a.count):
        gx, gz = i % 4, i // 4
        origin = (gx * a.spacing, 140, gz * a.spacing)
        b = FlyBot(a.host, a.port, f'{a.label}{i:03d}', proto, a.secret,
                   seed=i, hold=a.hold + 120, origin=origin)
        b.start()
        bots.append(b)
        time.sleep(0.22)

    # wait for joins
    t0 = time.time()
    while time.time() - t0 < 180:
        if sum(1 for b in bots if b.joined) >= a.count:
            break
        time.sleep(2)
    joined = sum(1 for b in bots if b.joined)
    print(f'[+] joined {joined}/{a.count}', flush=True)

    # creative + fly speed 2
    cmds = []
    for b in bots:
        if b.joined:
            cmds += [
                f'gamemode creative {b.name}',
                f'ability {b.name} mayfly true',
                f'ability {b.name} flying true',
                f'flyspeed {b.name} 2',
            ]
    for i in range(0, len(cmds), 24):
        console(*cmds[i:i + 24])
        time.sleep(0.4)
    print('[i] creative + flyspeed 2 applied', flush=True)

    # sample
    recs = []
    t1 = time.time()
    with open(a.out, 'a', encoding='utf-8') as f:
        while time.time() - t1 < a.hold:
            h = health()
            try:
                rs = resources()
            except Exception:
                rs = {}
            rec = {'label': a.label, 't': round(time.time() - t1, 1), **h, **rs}
            recs.append(rec)
            f.write(json.dumps(rec) + '\n')
            f.flush()
            print(f"    {a.label} players={h['players']} regions={h['regions']} "
                  f"tps={h['tps_min']} mspt={h['mspt_max']} "
                  f"rss={rs.get('rss_mb')}MB cpu={rs.get('cpu')}%", flush=True)
            time.sleep(a.sample)

    frames = sum(b.frames for b in bots)
    print(f'\n[=] {a.label} SUMMARY: joined={joined}/{a.count} move_frames={frames}',
          flush=True)
    tl = [r['tps_min'] for r in recs if r.get('tps_min') is not None]
    ml = [r['mspt_max'] for r in recs if r.get('mspt_max') is not None]
    rl = [r['rss_mb'] for r in recs if r.get('rss_mb') is not None]
    cl = [r['cpu'] for r in recs if r.get('cpu') is not None]
    gl = [r['regions'] for r in recs if r.get('regions') is not None]
    print(f"[=] regions max={max(gl) if gl else None} "
          f"tps min={min(tl) if tl else None} avg={sum(tl)/len(tl):.2f} "
          f"mspt max={max(ml) if ml else None} "
          f"rss max={max(rl) if rl else None} avg={sum(rl)/len(rl):.0f} "
          f"cpu avg={sum(cl)/len(cl):.1f} max={max(cl):.1f}" if tl and rl and cl else '[=] no samples')

    # disconnect bots
    for b in bots:
        try:
            if b.c:
                b.c.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
