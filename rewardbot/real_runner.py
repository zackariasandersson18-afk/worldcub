"""Real money with v4, run from the owner's own computer in Sweden (GitHub self-hosted runner).

Polymarket refuses orders from GitHub's servers (they are in the US: 403 'Trading restricted in your
region'). Everything else stays on GitHub; only this order-placing loop runs on the owner's Windows PC:
  start   asks polymarket.com/api/geoblock first and refuses to trade if this connection is blocked
          (no VPN or other way around a block is ever used)
  minute  downloads v4's current state from the rewards-state branch, runs rewardbot.real_live.tick
          (all its safety limits apply), and saves real_live.json back to rewards-state through the
          GitHub API, so the dashboard shows it
  end     cancels every order and starts the next run of this workflow
Python only (no bash, no git), so it runs the same on Windows.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

from rewardbot import real_live

REPO_API = 'https://api.github.com/repos/{repo}'
BRANCH = 'rewards-state'
SAVE_EVERY_S = 120


def gh(http, method: str, path: str, **kw):
    repo, token = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN']
    return http.request(method, REPO_API.format(repo=repo) + path, timeout=30,
                        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}, **kw)


def geoblock(http) -> dict:
    try:
        return http.get('https://polymarket.com/api/geoblock', timeout=20).json()
    except Exception as e:                                # noqa: BLE001
        return {'blocked': True, 'error': type(e).__name__}


def download(http, name: str, dest: Path) -> bool:
    r = gh(http, 'GET', f'/contents/rewards/{name}?ref={BRANCH}')
    if r.status_code != 200:
        return False
    body = r.json()
    if body.get('encoding') == 'base64' and body.get('content'):
        dest.write_bytes(base64.b64decode(body['content']))
    else:                                                 # files over 1 MB come without content: fetch the blob
        blob = gh(http, 'GET', f"/git/blobs/{body['sha']}")
        dest.write_bytes(base64.b64decode(blob.json()['content']))
    return True


def upload(http, src: Path, name: str, log=print) -> bool:
    path = f'/contents/rewards/{name}'
    for _ in range(3):
        cur = gh(http, 'GET', f'{path}?ref={BRANCH}')
        sha = cur.json().get('sha') if cur.status_code == 200 else None
        r = gh(http, 'PUT', path, json={'message': f'real live {time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())}',
                                        'content': base64.b64encode(src.read_bytes()).decode(), 'branch': BRANCH,
                                        **({'sha': sha} if sha else {})})
        if r.status_code in (200, 201):
            return True
        time.sleep(2)                                     # someone else committed in between: read the sha again
    log(f'real: save failed ({r.status_code})')
    return False


def next_run(http, log=print) -> None:
    ref = os.environ.get('GITHUB_REF_NAME') or 'main'
    r = gh(http, 'POST', '/actions/workflows/real-trading.yml/dispatches', json={'ref': ref})
    log(f'real: next run requested ({r.status_code})')


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.real_runner')
    ap.add_argument('--state', default='real_state')
    ap.add_argument('--minutes', type=float, default=345)
    a = ap.parse_args(argv)
    http = requests.Session()
    d = Path(a.state)
    d.mkdir(parents=True, exist_ok=True)
    g = geoblock(http)
    print(f"real: geoblock says blocked={g.get('blocked')} country={g.get('country')}")
    if g.get('blocked') is not False:
        print('real: Polymarket does not allow trading from this connection - not trading')
        return 0
    if not real_live.enabled():
        print('real: REAL_TRADING is not on, or the key/address secrets are missing - not trading')
        return 0
    cli = real_live.client()
    print(f"real: key address {cli.get_address()} · funder {os.environ.get('POLY_FUNDER')} · "
          f"signature type {os.environ.get('POLY_SIGNATURE_TYPE') or 1}")   # addresses are public; the key is never printed
    download(http, real_live.FILE, d / real_live.FILE)    # keep the loss-limit baseline and halt state across runs
    start, saved = time.time(), 0.0
    try:
        while time.time() - start < a.minutes * 60:
            t0 = time.time()
            try:
                if download(http, 'rewards_v4_state.json', d / 'rewards_v4_state.json'):
                    real_live.tick(str(d), http, log=print, cli=cli)
                if time.time() - saved >= SAVE_EVERY_S and (d / real_live.FILE).exists():
                    upload(http, d / real_live.FILE, real_live.FILE)
                    saved = time.time()
            except Exception as e:                        # noqa: BLE001  one bad minute must not end the run
                print(f'real: minute failed: {type(e).__name__}: {str(e)[:200]}')
            time.sleep(max(1.0, 60 - (time.time() - t0)))
    finally:
        real_live.cancel_everything(cli)
        if (d / real_live.FILE).exists():
            upload(http, d / real_live.FILE, real_live.FILE)
        next_run(http)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
