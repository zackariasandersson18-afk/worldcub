"""weatherbot CLI.

    python -m weatherbot backtest --days 45 --out weather_backtest.json
"""
from __future__ import annotations

import argparse
import json
import sys


def cmd_backtest(args) -> int:
    from weatherbot import backtest
    events, forecasts, histories = backtest.collect(args.days, args.pages, args.workers)
    res = backtest.run(events, forecasts, histories)
    trades = res.pop('trades')
    print(json.dumps(res, indent=2, default=str))
    if trades:
        import pandas as pd
        t = pd.DataFrame(trades)
        print('\nby side:\n', t.groupby('side')['pnl'].agg(['count', 'sum', 'mean']).round(4))
        print('\nby price band:\n', t.groupby(pd.cut(t['price'], [0, .1, .3, .5, .7, .9, 1]), observed=True)['pnl']
              .agg(['count', 'sum']).round(4))
        print('\nlast trades:\n', t.tail(10).to_string(index=False))
    if args.out:
        with open(args.out, 'w') as f:
            json.dump({**res, 'trades': trades}, f, indent=1, default=str)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='weatherbot')
    sub = p.add_subparsers(dest='cmd', required=True)
    sp = sub.add_parser('backtest', help='walk-forward backtest on resolved markets')
    sp.add_argument('--days', type=int, default=45)
    sp.add_argument('--pages', type=int, default=40, help='pages of 100 closed events to scan')
    sp.add_argument('--workers', type=int, default=8)
    sp.add_argument('--out')
    sp.set_defaults(func=cmd_backtest)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
