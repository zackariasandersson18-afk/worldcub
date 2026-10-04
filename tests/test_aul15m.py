from suisbot.aul15m import Market, decide, evaluate

S = 1_790_000_100


def mk(trades, up_won=True):
    return Market('m', S, up_won, 0.07, 1.0, sorted(trades))


def test_rule_follows_the_price_at_minute_13_and_skips_the_middle():
    assert decide(mk([(S + 700, 0.75, 5)]))['side'] == 'UP'
    d = decide(mk([(S + 700, 0.25, 5)], up_won=False))
    assert d['side'] == 'DOWN' and abs(d['price'] - 0.75) < 1e-9 and d['won']
    assert decide(mk([(S + 700, 0.55, 5)])) is None


def test_uses_only_trades_before_minute_13():
    tr = [(S + 650, 0.50, 5), (S + 790, 0.90, 5)]       # the 0.90 comes after the decision
    assert decide(mk(tr)) is None
    assert decide(mk([(S + 100, 0.90, 5)])) is None      # too early, no recent price


def test_costs_make_realistic_pnl_worse_than_theirs():
    d = decide(mk([(S + 700, 0.80, 5)], up_won=True))
    assert d['pnl_theirs'] > d['pnl_real'] > 0
    d = decide(mk([(S + 700, 0.80, 5)], up_won=False))
    assert d['pnl_theirs'] == d['pnl_real'] == -1.0


def test_evaluate_reports_breakeven_and_gates():
    trades = [decide(mk([(S + 700, 0.8, 1)], up_won=i % 5 != 0)) for i in range(20)]
    for i, t in enumerate(trades):
        t['day'] = __import__('datetime').date(2026, 9, 1 + i % 10)
    res = evaluate(trades, 20)
    assert 'gates' in res and res['breakeven_win_rate_real'] > 0.8
