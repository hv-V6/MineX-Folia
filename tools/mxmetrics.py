#!/usr/bin/env python3
"""Sample live server metrics from the Pterodactyl API during a load test.

Reads RSS / CPU / uptime from the panel and appends one JSON line per sample,
so a soak run can be correlated with memory and CPU afterwards.

usage:
  python mxmetrics.py --out samples.jsonl --interval 5 --duration 300
"""
import argparse
import json
import ssl
import time
import urllib.request

PANEL = 'panel.minex.gg'
SERVER = 'fd4afc59'
TOKEN = 'CHANGEME_PANEL_TOKEN'


def get(path):
    req = urllib.request.Request(
        f'https://{PANEL}{path}',
        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='samples.jsonl')
    ap.add_argument('--interval', type=float, default=5.0)
    ap.add_argument('--duration', type=float, default=300.0)
    a = ap.parse_args()

    t0 = time.time()
    n = 0
    with open(a.out, 'w', encoding='utf-8') as f:
        while time.time() - t0 < a.duration:
            try:
                r = get(f'/api/client/servers/{SERVER}/resources')['attributes']
                res = r['resources']
                rec = {
                    't': round(time.time() - t0, 1),
                    'state': r['current_state'],
                    'rss_mb': round(res['memory_bytes'] / 1048576, 1),
                    'cpu_pct': round(res['cpu_absolute'], 1),
                    'uptime_s': res['uptime'],
                    'net_rx': res['network_rx_bytes'],
                    'net_tx': res['network_tx_bytes'],
                }
                f.write(json.dumps(rec) + '\n')
                f.flush()
                n += 1
                print(f"[{rec['t']:>6.1f}s] rss={rec['rss_mb']:>7.1f}MB "
                      f"cpu={rec['cpu_pct']:>6.1f}% state={rec['state']}", flush=True)
            except Exception as e:
                print(f'[!] {type(e).__name__}: {e}', flush=True)
            time.sleep(a.interval)
    print(f'[=] wrote {n} samples to {a.out}')


if __name__ == '__main__':
    main()
