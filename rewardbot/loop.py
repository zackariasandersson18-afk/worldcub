"""Minute-by-minute runner for the liquidity-rewards measurements (virtual, read-only).

Polymarket scores resting orders every minute; the 30-minute ticks could not see
what happened in between. This loop runs versions 1 to 4 every minute for up
to --minutes (a GitHub-hosted job lives at most 6 hours), and commits the state
every --push-every minutes to its own branch (rewards-state), so it never races
the weather bots on tradebot-state. History keeps one row per 10 minutes.
"""
from __future__ import annotations

import os
import subprocess
import time
import traceback
from pathlib import Path

import requests

from memebot import paper as memebot_paper
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


CLOSE_PREFIX = 'memebot: stäng '


def close_requests(http, log=print) -> list[dict]:
    """Open issues titled 'memebot: stäng <pool>' written by the repository owner (anyone can open
    an issue on a public repository, so nobody else is obeyed)."""
    repo, token = os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GITHUB_TOKEN')
    if not repo or not token:
        return []
    owner = repo.split('/')[0]
    try:
        r = http.get(f'https://api.github.com/repos/{repo}/issues', params={'state': 'open', 'per_page': 50},
                     headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}, timeout=20)
        issues = r.json() if r.status_code == 200 else []
    except Exception:                                       # noqa: BLE001
        return []
    return [{'number': i['number'], 'pool': i['title'][len(CLOSE_PREFIX):].strip()} for i in issues
            if isinstance(i, dict) and 'pull_request' not in i and (i.get('title') or '').startswith(CLOSE_PREFIX)
            and (i.get('user') or {}).get('login') == owner]


def answer(http, number: int, text: str) -> None:
    repo, token = os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GITHUB_TOKEN')
    h = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    try:
        http.post(f'https://api.github.com/repos/{repo}/issues/{number}/comments', json={'body': text}, headers=h, timeout=20)
        http.patch(f'https://api.github.com/repos/{repo}/issues/{number}', json={'state': 'closed'}, headers=h, timeout=20)
    except Exception:                                       # noqa: BLE001
        pass


def meme_tick(state: str, http, log=print) -> dict:
    d = Path(state).parent / 'memebot'
    reqs = close_requests(http, log)
    t = memebot_paper.step_all(d, http=http, log=log, close={q["pool"] for q in reqs})
    if reqs:
        st = memebot_paper.load(d / 'memebot_state.json', {})
        done = {p['pool']: p for p in st.get('closed', []) if p.get('manual')}
        still = {p['pool'] for p in st.get('open', [])}
        for q in reqs:
            if q['pool'] in done:
                p = done[q['pool']]
                answer(http, q['number'], f"Stängd {p['closed'][:16].replace('T', ' ')} UTC: fick tillbaka {p['realised']:.0f} kr "
                                          f"av {p['stake']:.0f} kr ({p['pnl']:+.0f} kr).")
            elif q['pool'] not in still:
                answer(http, q['number'], 'Hittade ingen öppen affär med den poolen – den kan redan vara stängd.')
            # still open but no price this minute: try again next minute
    return t


def tick(state: str, http, log=print) -> None:
    for name, fn in (('v1', lambda: live.step(state, http=http, log=log)),
                     ('v2', lambda: live_v2.step(state, http=http, log=log, version='v2')),
                     ('v3', lambda: live_v2.step(state, http=http, log=log, version='v3')),
                     ('v4', lambda: live_v2.step(state, http=http, log=log, version='v4')),
                     ('meme', lambda: meme_tick(state, http, log))):
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
