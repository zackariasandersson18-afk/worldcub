"""Minute-by-minute runner for the liquidity-rewards measurements (virtual, read-only).

Polymarket scores resting orders every minute; the 30-minute ticks could not see
what happened in between. This loop runs versions 1, 2 and 3 every minute for up
to --minutes (a GitHub-hosted job lives at most 6 hours), and commits the state
every --push-every minutes to its own branch (rewards-state), so it never races
the weather bots on tradebot-state. History keeps one row per 10 minutes.
"""
from __future__ import annotations

import subprocess
import time
import traceback
from pathlib import Path

import requests

from rewardbot import live, live_v2


def git(repo: Path, *args: str) -> bool:
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True).returncode == 0


def push(repo: Path, branch: str, log=print) -> bool:
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=tradebot', '-c', 'user.email=tradebot@users.noreply.github.com',
        'commit', '-q', '-m', f'rewards loop {time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())}')
    for _ in range(3):
        if git(repo, 'push', '-q', 'origin', branch):
            return True
        git(repo, 'fetch', '-q', 'origin', branch)
        git(repo, 'rebase', '-q', f'origin/{branch}')
    log('state push failed')
    return False


def tick(state: str, http, log=print) -> None:
    for name, fn in (('v1', lambda: live.step(state, http=http, log=log)),
                     ('v2', lambda: live_v2.step(state, http=http, log=log, version='v2')),
                     ('v3', lambda: live_v2.step(state, http=http, log=log, version='v3'))):
        try:
            t = fn()
            log(f"{time.strftime('%H:%M:%S', time.gmtime())} {name} net {t.get('net')} share {t.get('avg_share')}")
        except Exception:                                  # noqa: BLE001  one failing version must not stop the others
            log(f'{name} failed:\n{traceback.format_exc(limit=3)}')


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='rewardbot.loop')
    ap.add_argument('--state', required=True, help='directory with the rewards state files')
    ap.add_argument('--repo', required=True, help='git worktree of the state branch')
    ap.add_argument('--branch', default='rewards-state')
    ap.add_argument('--minutes', type=float, default=345)
    ap.add_argument('--push-every', type=float, default=10)
    a = ap.parse_args(argv)
    http, start, pushed = requests.Session(), time.time(), time.time()
    while time.time() - start < a.minutes * 60:
        t0 = time.time()
        tick(a.state, http)
        if time.time() - pushed >= a.push_every * 60:
            push(Path(a.repo), a.branch)
            pushed = time.time()
        time.sleep(max(1.0, 60 - (time.time() - t0)))
    push(Path(a.repo), a.branch)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
