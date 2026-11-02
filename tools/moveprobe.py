#!/usr/bin/env python3
"""Probe which move-player packet variant the 26.3 server accepts.

The bot joins, accepts the initial teleport, then sends exactly ONE movement
packet and waits to see whether the server kicks it. Each variant is tried in a
fresh connection so one failure cannot mask the next.

usage: python moveprobe.py --host H --port P --secret S
"""
import argparse
import struct
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcjoin import Client, status_protocol, wv   # noqa: E402

CB_KEEPALIVE = 0x2d
CB_PLAYER_POSITION = 0x49
SB_KEEPALIVE = 0x1c
SB_ACCEPT_TELEPORT = 0x00
SB_MOVE_POS = 0x1e
SB_MOVE_POS_ROT = 0x1f
SB_MOVE_ROT = 0x20
SB_MOVE_STATUS = 0x21


def run_variant(host, port, proto, secret, name, pid, payload_desc, payload):
    c = Client(host, port, name, proto, quiet=True, secret=secret)
    c.connect()
    if not c.login_until_play(duration=90):
        return (name, payload_desc, 'no-play', None)
    # consume the initial teleport so we are in a settled position
    c.sock.settimeout(6)
    try:
        for _ in range(40):
            p, b = c.read_packet()
            if p == CB_PLAYER_POSITION:
                tid = b.vi()
                px, py, pz = struct.unpack('>ddd', b.read(24))
                b.read(24)
                yaw, pitch = struct.unpack('>ff', b.read(8))
                b.read(4)
                c.send(SB_ACCEPT_TELEPORT, wv(tid) + struct.pack('>ddd', px, py, pz)
                       + struct.pack('>ff', yaw, pitch))
                return finish(c, name, payload_desc, pid, payload, cx=px, cy=py, cz=pz)
            if p == CB_KEEPALIVE:
                c.send(SB_KEEPALIVE, b.read(8))
    except Exception as e:
        return (name, payload_desc, f'setup:{type(e).__name__}', None)
    return (name, payload_desc, 'no-teleport', None)


def finish(c, name, desc, pid, make_payload, cx, cy, cz):
    """Send one move packet, then listen for a kick for a few seconds."""
    time.sleep(0.4)
    try:
        c.send(pid, make_payload(cx, cy, cz))
    except Exception as e:
        return (name, desc, f'send:{type(e).__name__}', None)
    c.sock.settimeout(5)
    alive = True
    msg = 'no-kick'
    t0 = time.time()
    while time.time() - t0 < 6:
        try:
            p, b = c.read_packet()
        except Exception as e:
            msg = type(e).__name__
            alive = False
            break
        if p == CB_KEEPALIVE:
            c.send(SB_KEEPALIVE, b.read(8))
    c.close()
    return (name, desc, msg, alive)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--secret', default=None)
    a = ap.parse_args()

    proto = status_protocol(a.host, a.port)['version']['protocol']

    variants = [
        ('pos_flags0', SB_MOVE_POS,
         lambda x, y, z: struct.pack('>ddd', x, y, z) + b'\x00'),
        ('pos_flags2', SB_MOVE_POS,
         lambda x, y, z: struct.pack('>ddd', x, y, z) + b'\x02'),
        ('posrot_flags0', SB_MOVE_POS_ROT,
         lambda x, y, z: struct.pack('>dddff', x, y, z, 0.0, 0.0) + b'\x00'),
        ('posrot_flags2', SB_MOVE_POS_ROT,
         lambda x, y, z: struct.pack('>dddff', x, y, z, 0.0, 0.0) + b'\x02'),
        ('status_flags0', SB_MOVE_STATUS, lambda x, y, z: b'\x00'),
        ('status_flags2', SB_MOVE_STATUS, lambda x, y, z: b'\x02'),
        ('rot_flags2', SB_MOVE_ROT,
         lambda x, y, z: struct.pack('>ff', 0.0, 0.0) + b'\x02'),
    ]

    print(f'{"variant":<18} {"result":<22}')
    print('-' * 42)
    for i, (suffix, pid, mk) in enumerate(variants):
        # tiny offset so each packet is a real move, not identical
        r = run_variant(a.host, a.port, proto, a.secret,
                        f'probe{i}{suffix[0]}', pid, suffix,
                        lambda x, y, z, mk=mk, i=i: mk(x + 0.01 * (i + 1), y, z))
        print(f'{r[1]:<18} {r[2]:<22}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
