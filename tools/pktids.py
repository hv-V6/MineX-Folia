#!/usr/bin/env python3
"""Derive game packet ids from GameProtocols.java.

Packet ids are assigned purely by the order registrations appear in the
protocol builder, so the only reliable way to get them is to replay that order.

Two traps this handles:
  * `.withBundlePacket(...)` also consumes an id slot (it registers the bundle
    packet itself), which shifts every later id by one.
  * commented-out registrations must not be counted.

An earlier line-based attempt reported clientbound keep_alive as 0x2c when the
live server actually sends 0x2d; the bundle slot was the missing entry.

Ground truth: clientbound keep_alive is 0x2d, observed on the wire as an 8-byte
timestamp payload.
"""
import re
import sys

SRC = r'E:\minex\_folia\exp\folia-server\src\minecraft\java\net\minecraft\network\protocol\game\GameProtocols.java'

REG = re.compile(r'\.(addPacket|withBundlePacket)\(\s*([A-Za-z0-9_.]+)')

with open(SRC, encoding='utf-8') as f:
    text = f.read()


def collect(start_marker, end_marker):
    i = text.index(start_marker)
    j = text.index(end_marker, i + len(start_marker))
    body = text[i:j]
    body = re.sub(r'//[^\n]*', '', body)          # drop commented registrations
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    return REG.findall(body)


clientbound = collect('CLIENTBOUND_TEMPLATE =', 'public interface Context')
serverbound = collect('SERVERBOUND_TEMPLATE =', 'CLIENTBOUND_TEMPLATE =')

print(f'clientbound registrations: {len(clientbound)}')
print(f'serverbound registrations: {len(serverbound)}')


def name_of(n):
    return n.rsplit('.', 1)[-1]


print('\n--- clientbound (interesting) ---')
cb = {}
for i, (kind, n) in enumerate(clientbound):
    s = name_of(n)
    cb.setdefault(s, i)
    if any(w in s for w in ('KEEP_ALIVE', 'CLIENTBOUND_PING', 'CLIENTBOUND_LOGIN',
                            'START_CONFIGURATION', 'PLAYER_POSITION', 'RESPAWN',
                            'SET_HEALTH', 'SYSTEM_CHAT', 'BUNDLE')):
        print(f'  0x{i:02x}  {s}  ({kind})')

print('\n--- serverbound (interesting) ---')
sb = {}
for i, (kind, n) in enumerate(serverbound):
    s = name_of(n)
    sb.setdefault(s, i)
    if any(w in s for w in ('KEEP_ALIVE', 'PONG', 'MOVE_PLAYER', 'ACCEPT_TELEPORTATION',
                            'CLIENT_INFORMATION', 'CUSTOM_PAYLOAD')):
        print(f'  0x{i:02x}  {s}  ({kind})')

print('\n--- validation ---')
checks = [
    ('clientbound keep_alive', cb.get('CLIENTBOUND_KEEP_ALIVE'), 0x2d),
]
ok = True
for label, got, want in checks:
    good = got == want
    ok = ok and good
    gs = f'0x{got:02x}' if got is not None else 'MISSING'
    print(f'{label}: {gs} (expected 0x{want:02x}) {"PASS" if good else "FAIL"}')
sys.exit(0 if ok else 1)
