"""CLI for tradebot.

Examples:
    python -m tradebot demo
    python -m tradebot run --csv btc_daily.csv --n-trials 25
    python -m tradebot run --binance BTCUSDT --start 2019-01-01
    python -m tradebot size --capital 10000 --entry 60000 --stop 57000
    python -m tradebot dsr --sharpe 1.8 --n-trials 80 --n-obs 1095
    python -m tradebot health --returns live.csv --sharpe 1.2 --max-dd -25
    python -m tradebot prompt hypothesis --asset ETH --timeframe 1D

    # live: approve, then run once per closed bar (dry-run without --execute)
    python -m tradebot run --binance BTCUSDT --save-approval approval.json
    python -m tradebot trade --approval approval.json --broker paper --execute
    python -m tradebot trade --approval approval.json --broker testnet --execute
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial

import pandas as pd

from tradebot import strategies
from tradebot.broker import BinanceTestnetBroker, PaperBroker
from tradebot.critic import format_review
from tradebot.data import fetch_binance, load_csv, synthetic_prices
from tradebot.engine import Config, backtest, metrics
from tradebot.gates import GateThresholds, run_gates
from tradebot.live import LiveConfig, load_json, save_json, trade_step
from tradebot.monitor import health_check
from tradebot.prompts import TEMPLATES, render
from tradebot.regimes import regime_report
from tradebot.sizing import losing_streak_drawdown, position_size
from tradebot.stats import deflated_sharpe


def _fmt(d: dict) -> str:
    return ', '.join(f'{k}={v}' for k, v in d.items())


def _yes(ok: bool) -> str:
    return 'PASS' if ok else 'FAIL'


def run_pipeline(prices: pd.Series, args: argparse.Namespace) -> int:
    cfg = Config(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                 periods_per_year=args.periods_per_year)
    fit = partial(strategies.fit_momentum, cfg=cfg, allow_short=args.allow_short)
    n_trials = max(args.n_trials, len(strategies.LOOKBACK_GRID))

    print(f'Data: {len(prices)} bars, {prices.index[0]} -> {prices.index[-1]}')
    print(f'Strategy: time-series momentum, lookback grid {strategies.LOOKBACK_GRID}')
    print(f'Trials counted: {n_trials}\n')

    result = run_gates(prices, fit, cfg, n_trials,
                       train_days=args.train_days, test_days=args.test_days,
                       sources=(strategies,),
                       thresholds=GateThresholds(min_positive_folds=args.min_positive_folds,
                                                 min_worst_fold=args.min_worst_fold))

    print('== Critic: eight ways your backtest is lying ==')
    print(format_review(result['critic']))
    leak = result['leakage']
    print(f"Leakage test ({leak['cuts_tested']} cuts): {leak['status']}")
    for f in leak['failures']:
        print(f'     {f}')

    wf = result['walk_forward']
    print('\n== Walk-forward ==')
    cols = [c for c in ('start', 'params', 'sharpe', 'ann_return', 'max_drawdown')
            if c in wf['folds']]
    print(wf['folds'][cols].to_string(index=False))
    print(f"mean_sharpe={wf['mean_sharpe']}  positive_folds={wf['positive_folds']}  "
          f"worst_fold={wf['worst_fold']}")

    oos = result['oos_metrics']
    print('\n== Out-of-sample metrics (stitched folds) ==')
    print(_fmt(oos))
    if oos.get('red_flag'):
        print('RED FLAG: Sharpe above 2 on daily data means leakage until proven otherwise.')

    print('\n== Regimes (out-of-sample) ==')
    reg = regime_report(prices, wf['oos_net'], cfg)
    for name, m in reg['regimes'].items():
        print(f'{name}: {_fmt(m)}')
    print(reg['verdict'])

    print('\n== Multiple testing ==')
    print(_fmt(result['dsr']))

    print('\n== Gates ==')
    print(f"1. Critic finds no leakage:        {_yes(result['gate1_no_leakage'])}")
    print(f"2. Deflated Sharpe clears the bar: {_yes(result['gate2_deflated_sharpe'])}")
    print(f"3. Survives walk-forward:          {_yes(result['gate3_walk_forward'])}")

    if getattr(args, 'save_approval', None):
        latest = fit(prices.iloc[-args.train_days:])['params']
        save_json(args.save_approval, {
            'approved': result['approved'],
            'symbol': getattr(args, 'binance', None),
            'interval': getattr(args, 'interval', '1d'),
            'strategy': 'momentum',
            'params': latest,
            'allow_short': args.allow_short,
            'n_trials': n_trials,
            'oos_metrics': oos,
            'dsr': result['dsr'],
            'data_end': str(prices.index[-1]),
        })
        print(f'\nApproval file written: {args.save_approval} (approved={result["approved"]})')
    if result['approved']:
        print('\nAPPROVED for paper trading. Decide the kill condition now:')
        print(f"  halt if 30-period Sharpe < {oos['sharpe'] * 0.5:.2f} "
              f"or drawdown < {oos['max_drawdown'] * 1.5:.1f}%")
        return 0
    print('\nREJECTED. Do not trade this. That discovery is worth more than the strategy.')
    return 1


def cmd_demo(args):
    print('DEMO on synthetic prices -- illustrates the pipeline, proves nothing.\n')
    return run_pipeline(synthetic_prices(seed=args.seed), args)


def cmd_run(args):
    if args.csv:
        prices = load_csv(args.csv, args.time_col, args.price_col)
    else:
        prices = fetch_binance(args.binance, args.interval, args.start, args.end)
    return run_pipeline(prices, args)


def cmd_trade(args):
    approval = load_json(args.approval, {})
    if not approval:
        print(f'no approval file at {args.approval}; run `run --save-approval` first')
        return 1
    symbol = args.symbol or approval.get('symbol') or 'BTCUSDT'
    interval = approval.get('interval', '1d')
    start = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=args.history_days)).strftime('%Y-%m-%d')
    prices = fetch_binance(symbol, interval, start)

    if args.broker == 'paper':
        broker = PaperBroker(args.paper_wallet, symbol, args.paper_capital)
        broker.set_price(float(prices.iloc[-1]))
    else:
        broker = BinanceTestnetBroker(symbol)

    if args.ignore_gates and not approval.get('approved'):
        print('WARNING: --ignore-gates. This strategy did NOT pass the gates. '
              'Testnet/paper only -- never real money.')

    state = load_json(args.state, {})
    cfg = LiveConfig(risk_pct=args.risk_pct, max_position_pct=args.max_position_pct)
    report = trade_step(broker, prices, approval, state, cfg,
                        execute=args.execute, ignore_gates=args.ignore_gates)
    if args.execute:
        save_json(args.state, state)
    else:
        print('DRY RUN -- no order sent, state not changed. Add --execute.')
    print(json.dumps(report, indent=2, default=str))
    return {'HALT': 2, 'HALTED': 2, 'REFUSED': 1}.get(report['action'], 0)


def cmd_backtest(args):
    cfg = Config(fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                 periods_per_year=args.periods_per_year)
    prices = load_csv(args.csv) if args.csv else synthetic_prices()
    signal = strategies.momentum_signal(prices, args.lookback, args.allow_short)
    bt = backtest(prices, signal, cfg)
    print(_fmt(metrics(bt['net'], cfg)))
    print('In-sample, single parameter set. Not a verdict -- run `run` for the gates.')
    return 0


def cmd_size(args):
    print(_fmt(position_size(args.capital, args.entry, args.stop,
                             args.risk_pct, args.max_position_pct)))
    print(f'12 losses in a row at this risk: -{losing_streak_drawdown(args.risk_pct, 12)}%')
    return 0


def cmd_dsr(args):
    print(_fmt(deflated_sharpe(args.sharpe, args.n_trials, args.n_obs,
                               args.skew, args.kurtosis, args.periods_per_year)))
    return 0


def cmd_health(args):
    df = pd.read_csv(args.returns)
    res = health_check(df[args.column], {'sharpe': args.sharpe,
                                         'max_drawdown': args.max_dd},
                       window=args.window, periods_per_year=args.periods_per_year)
    print(_fmt(res))
    return 2 if res['action'] == 'HALT' else 0


def cmd_prompt(args):
    print(render(args.name, asset=args.asset, timeframe=args.timeframe,
                 n_trials=args.n_trials))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='tradebot', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def costs(sp):
        sp.add_argument('--fee-bps', type=float, default=5.0)
        sp.add_argument('--slippage-bps', type=float, default=3.0)
        sp.add_argument('--periods-per-year', type=int, default=365)
        sp.add_argument('--allow-short', action='store_true')

    def pipeline(sp):
        costs(sp)
        sp.add_argument('--n-trials', type=int, default=0,
                        help='every variation you tried. Be honest. '
                             '(minimum: size of the lookback grid)')
        sp.add_argument('--train-days', type=int, default=180)
        sp.add_argument('--test-days', type=int, default=60)
        sp.add_argument('--min-positive-folds', type=float, default=0.6)
        sp.add_argument('--min-worst-fold', type=float, default=-2.0)

    sp = sub.add_parser('demo', help='full pipeline on synthetic data')
    pipeline(sp)
    sp.add_argument('--seed', type=int, default=7)
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser('run', help='full pipeline: critic, walk-forward, DSR, gates')
    pipeline(sp)
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument('--csv')
    src.add_argument('--binance', metavar='SYMBOL')
    sp.add_argument('--time-col', default='timestamp')
    sp.add_argument('--price-col', default='close')
    sp.add_argument('--interval', default='1d')
    sp.add_argument('--start', default='2019-01-01')
    sp.add_argument('--end')
    sp.set_defaults(func=cmd_run)

    sp.add_argument('--save-approval', metavar='PATH',
                    help='write the gate result + latest params for `trade`')

    sp = sub.add_parser('trade', help='one live step on paper or the Binance testnet')
    sp.add_argument('--approval', required=True)
    sp.add_argument('--state', default='trade_state.json')
    sp.add_argument('--broker', choices=('paper', 'testnet'), default='paper')
    sp.add_argument('--symbol', help='defaults to the symbol in the approval file')
    sp.add_argument('--paper-wallet', default='paper_wallet.json')
    sp.add_argument('--paper-capital', type=float, default=10_000)
    sp.add_argument('--history-days', type=int, default=400)
    sp.add_argument('--risk-pct', type=float, default=0.01)
    sp.add_argument('--max-position-pct', type=float, default=0.20)
    sp.add_argument('--execute', action='store_true', help='actually send the order')
    sp.add_argument('--ignore-gates', action='store_true',
                    help='trade a strategy that failed the gates (testnet/paper only)')
    sp.set_defaults(func=cmd_trade)

    sp = sub.add_parser('backtest', help='single in-sample backtest')
    costs(sp)
    sp.add_argument('--csv')
    sp.add_argument('--lookback', type=int, default=60)
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser('size', help='position size from entry and stop')
    sp.add_argument('--capital', type=float, required=True)
    sp.add_argument('--entry', type=float, required=True)
    sp.add_argument('--stop', type=float, required=True)
    sp.add_argument('--risk-pct', type=float, default=0.01)
    sp.add_argument('--max-position-pct', type=float, default=0.20)
    sp.set_defaults(func=cmd_size)

    sp = sub.add_parser('dsr', help='deflated Sharpe ratio')
    sp.add_argument('--sharpe', type=float, required=True)
    sp.add_argument('--n-trials', type=int, required=True)
    sp.add_argument('--n-obs', type=int, required=True)
    sp.add_argument('--skew', type=float, default=0.0)
    sp.add_argument('--kurtosis', type=float, default=3.0)
    sp.add_argument('--periods-per-year', type=int, default=365)
    sp.set_defaults(func=cmd_dsr)

    sp = sub.add_parser('health', help='daily live health check (exit 2 = HALT)')
    sp.add_argument('--returns', required=True, help='CSV with live returns')
    sp.add_argument('--column', default='return')
    sp.add_argument('--sharpe', type=float, required=True, help='backtest Sharpe')
    sp.add_argument('--max-dd', type=float, required=True, help='backtest max DD in %%, e.g. -25')
    sp.add_argument('--window', type=int, default=30)
    sp.add_argument('--periods-per-year', type=int, default=365)
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser('prompt', help='print a role prompt')
    sp.add_argument('name', choices=sorted(TEMPLATES))
    sp.add_argument('--asset', default='BTC')
    sp.add_argument('--timeframe', default='4H')
    sp.add_argument('--n-trials', default='N')
    sp.set_defaults(func=cmd_prompt)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
