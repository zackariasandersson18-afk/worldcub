"""Live loop: one step per closed bar, on paper or the Binance testnet.

Run it once after every bar close (cron / systemd timer). Each step:
  1. refuses to trade without an approval that passed all three gates
  2. refuses stale data and bars it has already handled
  3. runs the health check -- HALT flattens the position and stops for good
  4. computes the signal from closed bars only
  5. sizes the position from a volatility stop (risk_pct per trade, capped)
  6. sends at most one market order, rounded to exchange rules
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from tradebot.monitor import health_check
from tradebot.sizing import position_size
from tradebot.strategies import momentum_signal


@dataclass
class LiveConfig:
    risk_pct: float = 0.01          # equity lost if the volatility stop is hit
    max_position_pct: float = 0.20  # hard cap on notional / equity
    stop_vol_mult: float = 2.0      # stop distance = mult * recent daily vol
    vol_window: int = 30
    rebalance_band: float = 0.25    # ignore size drift smaller than 25 % of target
    max_bar_age: pd.Timedelta = pd.Timedelta(days=2)
    health_window: int = 30
    periods_per_year: int = 365


def load_json(path: str | Path, default: dict) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def save_json(path: str | Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2, default=str))


def live_returns(history: list[dict]) -> pd.Series:
    """Equity returns per unit of exposure, so they are comparable with the
    backtest (which runs at 100 % exposure) and include the real costs."""
    out = []
    for prev, cur in zip(history, history[1:]):
        exp = prev['exposure']
        r = cur['equity'] / prev['equity'] - 1
        out.append(r / exp if exp > 0.01 else 0.0)
    return pd.Series(out, dtype=float)


def trade_step(broker, prices: pd.Series, approval: dict, state: dict,
               cfg: LiveConfig | None = None, execute: bool = False,
               ignore_gates: bool = False, now: pd.Timestamp | None = None) -> dict:
    """Returns a report. Mutates `state` only when execute=True."""
    cfg = cfg or LiveConfig()
    now = now or pd.Timestamp.now(tz='UTC')
    bar = prices.index[-1]
    report = {'bar': str(bar), 'action': 'NONE', 'executed': False}

    if not approval.get('approved') and not ignore_gates:
        return {**report, 'action': 'REFUSED',
                'reason': 'strategy did not pass all three gates'}
    if approval.get('allow_short'):
        return {**report, 'action': 'REFUSED', 'reason': 'spot account cannot short'}
    if state.get('halted'):
        return {**report, 'action': 'HALTED', 'reason': state.get('halt_reason')}
    if now - bar > cfg.max_bar_age:
        return {**report, 'action': 'REFUSED', 'reason': f'stale data: last bar {bar}'}
    if state.get('last_bar') == str(bar):
        return {**report, 'action': 'SKIP', 'reason': 'bar already handled'}

    rules = broker.rules()
    price = broker.price()
    bal = broker.balances()
    base_qty = bal.get(rules.base, 0.0)
    quote_qty = bal.get(rules.quote, 0.0)
    equity = quote_qty + base_qty * price
    exposure = base_qty * price / equity if equity > 0 else 0.0

    history = state.get('history', []) + [
        {'ts': str(bar), 'equity': equity, 'exposure': exposure}]
    health = health_check(live_returns(history), approval['oos_metrics'],
                          window=cfg.health_window,
                          periods_per_year=cfg.periods_per_year)

    signal = float(momentum_signal(prices, approval['params']['lookback']).iloc[-1])
    vol = float(prices.pct_change().tail(cfg.vol_window).std())
    stop = price * (1 - cfg.stop_vol_mult * vol)
    size = position_size(equity, price, stop, cfg.risk_pct, cfg.max_position_pct)
    target = 0.0 if health['action'] == 'HALT' else max(signal, 0.0) * size['units']

    report.update({'price': price, 'equity': round(equity, 2),
                   'signal': signal, 'stop': round(stop, 2),
                   'current_qty': base_qty, 'target_qty': round(target, 8),
                   'health': health})

    delta = target - base_qty
    side_change = (target == 0) != (base_qty * price < rules.min_notional)
    drift = abs(delta) / target if target > 0 else (1.0 if base_qty > 0 else 0.0)
    order = None
    if side_change or drift > cfg.rebalance_band:
        if delta > 0:
            affordable = quote_qty / (price * 1.002)  # headroom for fee/slippage
            qty = rules.round_qty(min(delta, affordable))
            side = 'BUY'
        else:
            qty = rules.round_qty(min(-delta, base_qty))
            side = 'SELL'
        if qty > 0 and qty >= rules.min_qty and qty * price >= rules.min_notional:
            order = {'side': side, 'qty': qty}
    report['order'] = order
    report['action'] = 'HALT' if health['action'] == 'HALT' else \
        (order['side'] if order else 'HOLD')

    if not execute:
        return report

    if order:
        fill = broker.market_order(order['side'], order['qty'])
        state.setdefault('trades', []).append({'ts': str(now), 'bar': str(bar),
                                               **asdict(fill)})
        report['fill'] = asdict(fill)
        bal = broker.balances()
        base_qty = bal.get(rules.base, 0.0)
        quote_qty = bal.get(rules.quote, 0.0)
        equity = quote_qty + base_qty * price
        history[-1]['exposure'] = base_qty * price / equity if equity > 0 else 0.0

    # snapshot for the dashboard: survives later SKIP/REFUSED reports
    state['account'] = {'base': base_qty, 'quote': quote_qty,
                        'price': price, 'bar': str(bar), 'asset': rules.base,
                        'quote_asset': rules.quote}
    state['last_decision'] = {'action': report['action'], 'signal': signal,
                              'stop': report['stop'], 'target_qty': report['target_qty'],
                              'health': health['action'], 'alerts': health['alerts']}
    state['history'] = history
    state['last_bar'] = str(bar)
    if health['action'] == 'HALT':
        state['halted'] = True
        state['halt_reason'] = ', '.join(health['alerts'])
    report['executed'] = True
    return report
