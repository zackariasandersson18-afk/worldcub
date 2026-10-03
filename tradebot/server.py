"""Always-on server mode: watch the market live instead of once a day.

What runs where:
  * every price tick (Binance WebSocket, REST polling as fallback):
      - protective stop: price <= stop  -> sell the whole position at once
      - kill switch: drawdown beyond the halt line -> sell and halt for good
  * once per day, shortly after the daily bar closes (00:00 UTC):
      - the normal decision step (tradebot.live.trade_step): signal, sizing,
        Sharpe-decay health check
  * state lives in a checkout of the `tradebot-state` branch and is pushed to
    GitHub on every trade and as a heartbeat, so the dashboard sees it.

The trading logic (LiveTrader) is synchronous and takes the clock and price as
arguments, so it is tested without a network; run_server() only wires it to the
price feed.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

import pandas as pd
import requests

from tradebot.live import LiveConfig, live_returns, load_json, save_json, trade_step
from tradebot.monitor import health_check

log = logging.getLogger('tradebot.server')

WS_URLS = (
    'wss://data-stream.binance.vision/ws/{s}@aggTrade',
    'wss://stream.binance.com:9443/ws/{s}@aggTrade',
)
TICKER_URLS = (
    'https://data-api.binance.vision/api/v3/ticker/price',
    'https://api.binance.com/api/v3/ticker/price',
)


@dataclass
class ServerConfig:
    daily_delay: pd.Timedelta = pd.Timedelta(seconds=30)  # let Binance finalize the daily bar
    retry: pd.Timedelta = pd.Timedelta(minutes=1)         # after a failed daily step
    heartbeat_s: float = 300                              # push state to GitHub at least this often
    save_s: float = 5                                     # write state to disk at least this often
    max_events: int = 50


class StateSync:
    """Commits the state directory (a git checkout of the state branch) and pushes it."""

    def __init__(self, directory: str | Path, branch: str = 'tradebot-state',
                 push: bool = True, run: Callable = subprocess.run):
        self.dir = str(directory)
        self.branch = branch
        self.enabled = push
        self.run = run

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(['git', '-C', self.dir, *args], capture_output=True, text=True)

    def push(self, message: str) -> bool:
        self._git('add', '-A')
        commit = self._git('-c', 'user.name=tradebot', '-c',
                           'user.email=tradebot@users.noreply.github.com',
                           'commit', '-q', '-m', message)
        if commit.returncode != 0:
            return False  # nothing to commit
        if not self.enabled:
            return True
        res = self._git('push', '-q', 'origin', f'HEAD:{self.branch}')
        if res.returncode != 0:
            log.warning('state push failed: %s', res.stderr.strip())
            return False
        return True


def ensure_checkout(directory: str | Path, repo: str | None, branch: str = 'tradebot-state',
                    token: str | None = None) -> None:
    """Clone the state branch into `directory`, or pull it if it is already there."""
    d = Path(directory)
    if (d / '.git').exists():
        subprocess.run(['git', '-C', str(d), 'pull', '-q', '--ff-only', 'origin', branch], check=False)
        return
    if not repo:
        d.mkdir(parents=True, exist_ok=True)
        return
    auth = f'x-access-token:{token}@' if token else ''
    url = f'https://{auth}github.com/{repo}.git'
    subprocess.run(['git', 'clone', '-q', '--branch', branch, '--single-branch', url, str(d)], check=True)


class LiveTrader:
    def __init__(self, broker, approval: dict, state_dir: str | Path,
                 fetch_prices: Callable[[], pd.Series], sync: StateSync | None = None,
                 cfg: LiveConfig | None = None, scfg: ServerConfig | None = None,
                 ignore_gates: bool = False):
        self.broker = broker
        self.approval = approval
        self.dir = Path(state_dir)
        self.state_path = self.dir / 'trade_state.json'
        self.state = load_json(self.state_path, {})
        self.fetch_prices = fetch_prices
        self.sync = sync
        self.cfg = cfg or LiveConfig()
        self.scfg = scfg or ServerConfig()
        self.ignore_gates = ignore_gates
        self.last_price: float | None = None
        self._done_bar: pd.Timestamp | None = None
        self._next_try: pd.Timestamp | None = None
        self._saved_at = 0.0
        self._pushed_at = 0.0
        # state written before the stop was persisted: use the last computed stop
        if (self.state.get('account') or {}).get('base', 0) > 0 and not self.state.get('stop'):
            self.state['stop'] = (self.state.get('last_decision') or {}).get('stop')

    # ------------------------------------------------------------ persistence
    def _save(self, message: str, push_now: bool = False) -> None:
        now = time.monotonic()
        if not push_now and now - self._saved_at < self.scfg.save_s:
            return
        save_json(self.state_path, self.state)
        self._saved_at = now
        if self.sync and (push_now or now - self._pushed_at >= self.scfg.heartbeat_s):
            if self.sync.push(message) or not push_now:
                self._pushed_at = now

    def _event(self, now: pd.Timestamp, kind: str, detail: dict) -> dict:
        ev = {'ts': str(now), 'kind': kind, **detail}
        events = self.state.setdefault('events', [])
        events.append(ev)
        del events[:-self.scfg.max_events]
        log.info('%s %s', kind, detail)
        return ev

    # ------------------------------------------------------------ every tick
    def on_tick(self, price: float, now: pd.Timestamp) -> dict | None:
        self.last_price = price
        if hasattr(self.broker, 'set_price'):
            self.broker.set_price(price)
        st = self.state
        event = None

        acct = st.get('account') or {}
        qty = acct.get('base', 0.0)
        if not st.get('halted') and qty > 0:
            stop = st.get('stop')
            if stop and price <= stop:
                event = self._flatten(now, 'STOP', f'price {price:.2f} <= stop {stop:.2f}')
            elif self.approval.get('oos_metrics'):
                equity = acct.get('quote', 0.0) + qty * price
                hist = st.get('history', []) + [
                    {'ts': 'live', 'equity': equity, 'exposure': qty * price / equity if equity else 0.0}]
                h = health_check(live_returns(hist), self.approval['oos_metrics'],
                                 window=self.cfg.health_window,
                                 periods_per_year=self.cfg.periods_per_year)
                # Sharpe decay needs whole days and is judged by the daily step;
                # the drawdown kill switch acts immediately.
                if 'DRAWDOWN_EXCEEDED' in h['alerts']:
                    event = self._flatten(now, 'HALT', f"drawdown {h['current_dd']}%")
                    st['halted'] = True
                    st['halt_reason'] = 'DRAWDOWN_EXCEEDED (intraday)'

        st['server'] = {'heartbeat': str(now), 'price': price, 'stop': st.get('stop'),
                        'watching': bool(st.get('stop')) and not st.get('halted')}
        self._save(f'tradebot {event["kind"]} {now:%Y-%m-%d %H:%M}' if event else
                   f'tradebot heartbeat {now:%Y-%m-%d %H:%M}', push_now=event is not None)
        return event

    def _flatten(self, now: pd.Timestamp, kind: str, reason: str) -> dict | None:
        rules = self.broker.rules()
        bal = self.broker.balances()
        qty = rules.round_qty(bal.get(rules.base, 0.0))
        price = self.last_price
        if qty <= 0 or qty < rules.min_qty or qty * price < rules.min_notional:
            return None
        fill = self.broker.market_order('SELL', qty)
        st = self.state
        st.setdefault('trades', []).append(
            {'ts': str(now), 'bar': 'intraday', 'reason': kind, **asdict(fill)})
        bal = self.broker.balances()
        st['account'] = {'base': bal.get(rules.base, 0.0), 'quote': bal.get(rules.quote, 0.0),
                         'price': price, 'bar': 'intraday', 'asset': rules.base,
                         'quote_asset': rules.quote}
        st['stop'] = None
        st['last_decision'] = {**st.get('last_decision', {}), 'action': kind}
        return self._event(now, kind, {'reason': reason, 'fill': asdict(fill)})

    # ------------------------------------------------------------ once a day
    def maybe_daily(self, now: pd.Timestamp) -> dict | None:
        due = now.floor('D')  # close time of the newest finished daily bar
        if now - due < self.scfg.daily_delay or self._done_bar == due:
            return None
        if self.state.get('last_bar') and pd.Timestamp(self.state['last_bar']) >= due:
            self._done_bar = due
            return None
        if self._next_try and now < self._next_try:
            return None
        try:
            prices = self.fetch_prices()
        except Exception as exc:  # network: try again shortly
            log.warning('daily step: price fetch failed: %s', exc)
            self._next_try = now + self.scfg.retry
            return None
        if prices.index[-1] < due:
            self._next_try = now + self.scfg.retry  # Binance has not published the bar yet
            return None
        if hasattr(self.broker, 'set_price'):
            self.broker.set_price(self.last_price or float(prices.iloc[-1]))

        report = trade_step(self.broker, prices, self.approval, self.state, self.cfg,
                            execute=True, ignore_gates=self.ignore_gates, now=now)
        self._done_bar = due  # REFUSED/HALTED are final for this bar too
        (self.dir / 'last_run.txt').write_text(json.dumps(report, indent=2, default=str))
        self._event(now, 'DAILY', {'action': report['action'], 'bar': report['bar']})
        self._save(f'tradebot daily {report["action"]} {due:%Y-%m-%d}', push_now=True)
        return report


# ---------------------------------------------------------------- price feed
async def price_stream(symbol: str, poll_s: float = 2.0) -> AsyncIterator[tuple[float, pd.Timestamp]]:
    """Yields (price, trade time). WebSocket first; REST polling while it is down."""
    import websockets

    s = symbol.lower()
    attempt = 0
    while True:
        url = WS_URLS[attempt % len(WS_URLS)].format(s=s)
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          open_timeout=15) as ws:
                log.info('price feed: %s', url)
                attempt = 0
                async for msg in ws:
                    d = json.loads(msg)
                    yield float(d['p']), pd.Timestamp(d['T'], unit='ms', tz='UTC')
        except Exception as exc:
            attempt += 1
            log.warning('websocket %s failed (%s); polling REST for a minute', url, exc)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for base in TICKER_URLS:
                try:
                    r = await asyncio.to_thread(requests.get, base, params={'symbol': symbol}, timeout=10)
                    r.raise_for_status()
                    yield float(r.json()['price']), pd.Timestamp.now(tz='UTC')
                    break
                except Exception:
                    continue
            await asyncio.sleep(poll_s)


async def run_server(trader: LiveTrader, symbol: str) -> None:
    async def clock():
        # the daily step and heartbeat must run even if the feed goes quiet
        while True:
            await asyncio.sleep(10)
            await asyncio.to_thread(trader.maybe_daily, pd.Timestamp.now(tz='UTC'))

    clock_task = asyncio.create_task(clock())
    try:
        async for price, _ in price_stream(symbol):
            now = pd.Timestamp.now(tz='UTC')
            await asyncio.to_thread(trader.on_tick, price, now)
    finally:
        clock_task.cancel()


def single_instance(state_dir: str | Path):
    """Exclusive lock so two servers never trade the same account."""
    f = open(Path(state_dir) / '.server.lock', 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('another tradebot server is already running on this state directory')
    return f


def main(args) -> int:
    from tradebot.broker import BinanceTestnetBroker, PaperBroker
    from tradebot.data import fetch_binance

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('GITHUB_TOKEN')
    ensure_checkout(args.state_dir, args.repo, token=token)
    lock = single_instance(args.state_dir)  # noqa: F841 (held for the process lifetime)

    state_dir = Path(args.state_dir)
    approval = load_json(state_dir / 'approval.json', {})
    if not approval:
        raise SystemExit(f'no approval.json in {state_dir}; run the daily workflow once '
                         'or `python -m tradebot run --binance BTCUSDT --save-approval '
                         f'{state_dir}/approval.json`')
    symbol = args.symbol or approval.get('symbol') or 'BTCUSDT'
    keys = os.environ.get('BINANCE_TESTNET_API_KEY') and os.environ.get('BINANCE_TESTNET_API_SECRET')
    args.broker = args.broker or ('testnet' if keys else 'paper')
    if args.broker == 'paper':
        broker = PaperBroker(state_dir / 'paper_wallet.json', symbol)
    else:
        broker = BinanceTestnetBroker(symbol)

    def fetch_prices():
        start = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=400)).strftime('%Y-%m-%d')
        return fetch_binance(symbol, approval.get('interval', '1d'), start)

    sync = StateSync(state_dir, push=not args.no_push and (state_dir / '.git').exists())
    trader = LiveTrader(broker, approval, state_dir, fetch_prices, sync,
                        cfg=LiveConfig(risk_pct=args.risk_pct, max_position_pct=args.max_position_pct),
                        ignore_gates=args.ignore_gates)
    if args.ignore_gates and not approval.get('approved'):
        log.warning('--ignore-gates: trading a strategy that FAILED the gates (paper/testnet only)')
    log.info('tradebot server: %s on %s, state %s, push=%s', symbol, args.broker,
             state_dir, sync.enabled)
    def stop(*_):  # docker stop / systemctl stop send SIGTERM: save before exiting
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(run_server(trader, symbol))
    except KeyboardInterrupt:
        pass
    finally:
        save_json(trader.state_path, trader.state)
    return 0
