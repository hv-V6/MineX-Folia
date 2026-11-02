#!/usr/bin/env python3
"""Sweep Folia tick-thread counts under a fixed 80-bot creative-flight load.

For each thread count: set `threaded-regions.threads` in paper-global.yml,
restart the server, relaunch the bot fleet, and sample per-region MSPT + TPS.
Server restarts kill the bots, so the fleet is relaunched per stage.

Instrumentation note: spark's Folia adapter is broken on 26.3
(NoClassDefFoundError: TickData$TickReportData), so this uses the server's own
`paper:tps server 5` health report, which gives per-region MSPT directly. That
is the right metric for region scheduling anyway: it is the cost of the tick the
region scheduler actually ran.

usage:
  python threadsweep.py --stages 3,6,12 --bots 80 --hold 150
"""
import argparse
import json
import os
import re
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SFTP_DIR = r'E:\minex\_tools\sftp'
TOOLS = HERE
PANEL = 'panel.minex.gg'
SERVER = 'fd4afc59'
TOKEN = 'CHANGEME_PANEL_TOKEN'
SFTP_USER = 'CHANGEME_SFTP_USER'

PG_REMOTE = '/config/paper-global.yml'
PG_LOCAL = r'E:\minex\_tmp_sweep_pg.yml'


def api(path, method='GET', body=None, retries=4):
    """Panel API with retry: the panel intermittently returns 502/504."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f'https://{PANEL}{path}', method=method,
                data=(json.dumps(body).encode() if body else None),
                headers={'Authorization': f'Bearer {TOKEN}',
                         'Accept': 'application/json',
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=40,
                                        context=ssl.create_default_context()) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (502, 503, 504):
                raise
            time.sleep(4 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(4 * (attempt + 1))
    raise last


def state():
    return api(f'/api/client/servers/{SERVER}/resources')['attributes']


def wait_state(target, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if state()['current_state'] == target:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def sftp(*args, env_user=SFTP_USER):
    e = dict(os.environ)
    e['MX_SFTP_USER'] = env_user
    r = subprocess.run(['node', 'sftp.js', *args], cwd=SFTP_DIR,
                       capture_output=True, text=True, timeout=240, env=e)
    return r.stdout + r.stderr


def console(*cmds):
    e = dict(os.environ)
    e['MX_SERVER'] = SERVER
    r = subprocess.run(['node', 'console.js', *cmds], cwd=SFTP_DIR,
                       capture_output=True, text=True, timeout=240, env=e)
    return r.stdout + r.stderr


def set_threads(n):
    """Rewrite threaded-regions.threads in paper-global.yml and upload."""
    sftp('get', PG_REMOTE, PG_LOCAL)
    with open(PG_LOCAL, encoding='utf-8', newline='') as f:
        text = f.read()
    # Only touch the threads key inside threaded-regions.
    new, cnt = re.subn(r'(?m)^(threaded-regions:\n(?:[ \t]+.*\n)*?[ \t]+threads:[ \t]*)\S+',
                       lambda m: m.group(1) + str(n), text, count=1)
    if cnt != 1:
        raise SystemExit(f'failed to patch threads= (matched {cnt})')
    with open(PG_LOCAL, 'w', encoding='utf-8', newline='') as f:
        f.write(new)
    out = sftp('put', PG_LOCAL, PG_REMOTE)
    return out


def sample(n_samples, interval):
    """Sample paper:tps and collect per-region MSPT + TPS."""
    mspt, tps, regions, util = [], [], [], []
    for _ in range(n_samples):
        o = console('paper:tps server 5')
        for m in re.finditer(r'([\d.]+) MSPT at ([\d.]+) TPS', o):
            mspt.append(float(m.group(1)))
            tps.append(float(m.group(2)))
        r = re.search(r'Total regions:\s*(\d+)', o)
        if r:
            regions.append(int(r.group(1)))
        u = re.search(r'Utilisation:\s*([\d.]+)%', o)
        if u:
            util.append(float(u.group(1)))
        time.sleep(interval)
    return mspt, tps, regions, util


def pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * p))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stages', default='3,6,12')
    ap.add_argument('--bots', type=int, default=80)
    ap.add_argument('--hold', type=int, default=150)
    ap.add_argument('--settle', type=int, default=70)
    ap.add_argument('--samples', type=int, default=8)
    ap.add_argument('--out', default='threadsweep.jsonl')
    a = ap.parse_args()
    stages = [int(x) for x in a.stages.split(',')]

    results = []
    for threads in stages:
        print(f'\n===== threads={threads} =====', flush=True)

        print('[i] setting threads...', flush=True)
        print(set_threads(threads).strip()[:200], flush=True)

        print('[i] restarting server...', flush=True)
        api(f'/api/client/servers/{SERVER}/power', 'POST', {'signal': 'restart'})
        wait_state('offline', 180)
        wait_state('running', 300)
        time.sleep(a.settle)

        # relaunch the fleet
        print(f'[i] launching {a.bots} bots...', flush=True)
        log = os.path.join(TOOLS, f'_sweep_{threads}.log')
        lf = open(log, 'w', encoding='utf-8')
        p = subprocess.Popen(
            [sys.executable, 'abflight.py', '--host', '208.84.101.13',
             '--port', '35581', '--secret', 'ZTk0MTdkOTAz',
             '--count', str(a.bots), '--hold', str(a.hold),
             '--label', f't{threads}', '--radius', '260', '--cell', '900',
             '--sample', '300', '--out', a.out],
            cwd=TOOLS, stdout=lf, stderr=subprocess.STDOUT)
        time.sleep(max(60, a.bots * 1.2))   # let the fleet reach play

        mspt, tps, regions, util = sample(a.samples, 8)

        rec = {
            'threads': threads,
            'regions': max(regions) if regions else None,
            'util_max': max(util) if util else None,
            'mspt_med': statistics.median(mspt) if mspt else None,
            'mspt_p95': pct(mspt, 0.95),
            'mspt_max': max(mspt) if mspt else None,
            'tps_min': min(tps) if tps else None,
            'n': len(mspt),
        }
        results.append(rec)
        print(f"[=] threads={threads} regions={rec['regions']} "
              f"util={rec['util_max']}% mspt med={rec['mspt_med']} "
              f"p95={rec['mspt_p95']} max={rec['mspt_max']} "
              f"tps_min={rec['tps_min']}", flush=True)

        p.terminate()
        try:
            p.wait(timeout=20)
        except Exception:
            p.kill()
        lf.close()
        time.sleep(20)

    print('\n===== SWEEP RESULTS =====')
    print(f"{'threads':>8} {'regions':>8} {'util%':>7} {'mspt_med':>9} "
          f"{'mspt_p95':>9} {'mspt_max':>9} {'tps_min':>8}")
    for r in results:
        print(f"{r['threads']:>8} {str(r['regions']):>8} "
              f"{str(r['util_max']):>7} {str(r['mspt_med']):>9} "
              f"{str(r['mspt_p95']):>9} {str(r['mspt_max']):>9} "
              f"{str(r['tps_min']):>8}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
