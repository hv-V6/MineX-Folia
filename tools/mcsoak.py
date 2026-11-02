#!/usr/bin/env python3
"""Multi-bot soak/load harness for Folia 26.3.

Bots join through the real Velocity modern-forwarding path (same as production),
hold the connection, answer keepalives and accept teleports. They deliberately
do NOT send movement packets on their own:

  * ServerGamePacketListenerImpl.handleMovePlayer kicks a client that sends a
    second position packet inside one tick, and an anticheat (Vulcan) rejects
    client-side movement that has no valid physics behind it. A synthetic walk
    loop trips both, so bots stay put and are positioned server-side instead.

Positioning is done afterwards, from the console, with /tp -- that is also the
more useful test, because it drives cross-region handoffs in the scheduler.

usage:
  python mcsoak.py --host 208.84.101.13 --port 35581 --count 10 \
      --secret <velocity-secret> --stay 120
"""
import argparse
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcjoin import Client, status_protocol   # noqa: E402


class Bot(threading.Thread):
    daemon = True

    def __init__(self, host, port, name, protocol, secret, stay, out):
        super().__init__(name=name)
        self.host, self.port, self.name = host, port, name
        self.protocol, self.secret, self.stay = protocol, secret, stay
        self.out = out
        self.joined = False
        self.ok = False
        self.err = None
        self.c = None
        self.keepalives = 0
        self.teleports = 0

    def run(self):
        try:
            self.c = Client(self.host, self.port, self.name, self.protocol,
                            quiet=True, secret=self.secret)
            self.c.connect()
            if not self.c.login_until_play(duration=90):
                self.err = 'never reached play'
                return
            self.joined = True
            # stay() is bounded; keep looping so the bot can outlive one window
            end = time.time() + self.stay
            while time.time() < end:
                remaining = max(1, int(min(30, end - time.time())))
                if not self.c.stay(remaining, move=False):
                    self.err = 'dropped'
                    return
            self.ok = True
        except Exception as e:
            self.err = f'{type(e).__name__}: {e}'
        finally:
            self.keepalives = getattr(self.c, 'keepalives', 0) if self.c else 0
            self.teleports = getattr(self.c, 'teleports', 0) if self.c else 0
            if self.c:
                self.c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--count', type=int, default=5)
    ap.add_argument('--prefix', default='SoakBot')
    ap.add_argument('--secret', default=None)
    ap.add_argument('--stay', type=int, default=120)
    ap.add_argument('--stagger', type=float, default=0.25,
                    help='seconds between joins (avoids a login stampede)')
    a = ap.parse_args()

    st = status_protocol(a.host, a.port)
    proto = st['version']['protocol']
    print(f'[i] {st["version"]["name"]} protocol={proto} '
          f'players={st["players"]["online"]}/{st["players"]["max"]}', flush=True)

    bots = [Bot(a.host, a.port, f'{a.prefix}{i:02d}', proto, a.secret, a.stay, None)
            for i in range(1, a.count + 1)]

    for b in bots:
        b.start()
        time.sleep(a.stagger)

    # Report progress until the window closes.
    end = time.time() + a.stay + 80
    while time.time() < end and any(b.is_alive() for b in bots):
        joined = sum(1 for b in bots if b.joined)
        alive = sum(1 for b in bots if b.is_alive() and b.joined and not b.err)
        print(f'[.] t={int(time.time() - (end - a.stay - 80)):>3}s '
              f'joined={joined}/{a.count} healthy={alive}', flush=True)
        time.sleep(10)

    joined = sum(1 for b in bots if b.joined)
    ok = sum(1 for b in bots if b.ok)
    ka = sum(b.keepalives for b in bots)
    tp = sum(b.teleports for b in bots)
    print(f'\n[=] joined={joined}/{a.count} completed={ok}')
    print(f'[=] total keepalives={ka} teleports_accepted={tp}')
    errs = {}
    for b in bots:
        if b.err:
            errs[b.err] = errs.get(b.err, 0) + 1
    if errs:
        print('[=] failures:')
        for k, v in sorted(errs.items(), key=lambda kv: -kv[1]):
            print(f'      {v:>3} x {k}')
    return 0 if ok == a.count else 1


if __name__ == '__main__':
    sys.exit(main())
