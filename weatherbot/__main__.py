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
    cal_rows = res.pop('calibration_rows')
    if args.state:
        from pathlib import Path
        d = Path(args.state)
        d.mkdir(parents=True, exist_ok=True)
        (d / 'backtest.json').write_text(json.dumps({**res, 'n_trades': len(trades)}, indent=1, default=str))
        cal = d / 'calibration.json'
        if not cal.exists():  # live residuals take over once paper trading runs
            cal.write_text(json.dumps({'rows': cal_rows}))
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


def cmd_paper(args) -> int:
    from pathlib import Path
    from weatherbot import paper
    bt = Path(args.state) / 'backtest.json'
    approved = json.loads(bt.read_text()).get('approved', False) if bt.exists() else False
    ignored = args.ignore_gates and not approved
    if ignored:
        print('WARNING: --ignore-gates: weather strategy did NOT pass the gates. Virtual money only.')
        approved = True
    rep = paper.step(args.state, approved=approved, gates_ignored=ignored)
    print(json.dumps({k: rep[k] for k in ('ts', 'logged', 'skipped')}, indent=1, default=str))
    for p in rep['opened']:
        print(f"OPEN  {p['city']:15s} {p['bucket']:18s} {p['side']:3s} @ {p['price']:.3f} "
              f"p={p['prob']:.2f} edge={p['edge']:.2f} ${p['stake_usd']}")
    for p in rep['settled']:
        print(f"SETTLE {p['city']:15s} {p['bucket']:18s} {p['side']:3s} {'WON' if p['won'] else 'lost'} "
              f"{p['pnl_usd']:+.2f}")
    return 0


def cmd_nowcast_paper(args) -> int:
    from weatherbot import nowcast_paper
    rep = nowcast_paper.step(args.state)
    print(json.dumps({k: rep[k] for k in ('ts', 'decided', 'skipped')}, indent=1, default=str))
    for p in rep['opened']:
        print(f"OPEN  {p['city']:15s} {p['bucket']:18s} {p['side']:3s} @ {p['price']:.3f} "
              f"seen={p['observed']} p={p['prob']:.2f} edge={p['edge']:.2f} ${p['stake_usd']}")
    for p in rep['settled']:
        print(f"SETTLE {p['city']:15s} {p['bucket']:18s} {p['side']:3s} {'WON' if p['won'] else 'lost'} "
              f"{p['pnl_usd']:+.2f}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='weatherbot')
    sub = p.add_subparsers(dest='cmd', required=True)
    sp = sub.add_parser('backtest', help='walk-forward backtest on resolved markets')
    sp.add_argument('--days', type=int, default=45)
    sp.add_argument('--pages', type=int, default=40, help='pages of 100 closed events to scan')
    sp.add_argument('--workers', type=int, default=8)
    sp.add_argument('--out')
    sp.add_argument('--state', help='state dir: write backtest.json and seed calibration.json')
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser('paper', help='one paper-trading step on live markets')
    sp.add_argument('--state', default='state/weather')
    sp.add_argument('--ignore-gates', action='store_true')
    sp.set_defaults(func=cmd_paper)

    sp = sub.add_parser('nowcast-paper', help='one paper step of the 16:00 solar nowcast')
    sp.add_argument('--state', default='state/nowcast')
    sp.set_defaults(func=cmd_nowcast_paper)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
