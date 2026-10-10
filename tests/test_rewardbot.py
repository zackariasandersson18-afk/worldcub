from rewardbot.backtest import book_q, expected_reward, qmin, score, simulate_fills


def test_score_matches_polymarket_formula():
    assert score(0, 3.0, 100) == 100 and abs(score(1.5, 3.0, 100) - 25) < 1e-9 and score(3.0, 3.0, 100) == 0
    assert qmin(90, 30) == 30 and qmin(90, 0) == 30          # one-sided counts a third


def test_reward_share_falls_with_competition():
    alone = expected_reward(10.0, 3.0, 100, 1.0, (0.0, 0.0))
    crowded = expected_reward(10.0, 3.0, 100, 1.0, (1000.0, 1000.0))
    assert abs(alone - 10.0) < 1e-9 and crowded < alone / 10


def test_book_q_counts_yes_bids_and_no_asks_on_the_same_side():
    yes = {'bids': [{'price': '0.49', 'size': '100'}], 'asks': [{'price': '0.51', 'size': '100'}]}
    no = {'bids': [], 'asks': [{'price': '0.51', 'size': '100'}]}      # NO ask 0.51 = YES bid 0.49
    q1, q2 = book_q(yes, no, 0.50, 3.0)
    assert abs(q1 - 2 * q2) < 1e-9


def test_fills_only_through_the_quote_and_marks_daily():
    t0 = 1_790_000_000 // 86400 * 86400
    trades = [(t0 + 10, 0.50, 5)] + [(t0 + 3600 * k + 10, 0.47 if k % 2 else 0.53, 50) for k in range(1, 24)]
    pnl = simulate_fills(trades, t0, t0 + 2 * 86400, 0.02, 10)
    assert pnl.sum() > 0
    flat = [(t0 + 10, 0.50, 5)] + [(t0 + 3600 * k + 10, 0.48, 50) for k in range(1, 24)]
    assert simulate_fills(flat, t0, t0 + 86400, 0.02, 10).sum() == 0     # touching is not a fill


def test_live_step_freezes_portfolio_accrues_reward_and_fills(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live
    port = [{'cond': 'c1', 'question': 'Q', 'yes': 'y', 'no': 'n', 'rate': 24.0, 'v': 4.0, 'min_size': 10.0,
             'd': 0.02, 'asset': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', 'collateral': 9.6,
             'est_reward_day': 24.0, 'end': '2027-01-01'}]
    monkeypatch.setattr(live, 'choose', lambda http, log: port)
    book = {'y': {'bids': [{'price': '0.49', 'size': '5'}], 'asks': [{'price': '0.51', 'size': '5'}]},
            'n': {'bids': [], 'asks': []}}
    monkeypatch.setattr(bt, 'fetch_books', lambda toks, http: book)
    t0 = 1_790_000_000
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: [(t0 + 100, 0.47, 50)])
    live.step(tmp_path, now=t0, http=object())
    tick = live.step(tmp_path, now=t0 + 3600, http=object())
    # alone in the book (competitor levels below min size): full share, 1 h of a 24/day pool
    assert abs(tick['reward_tick'] - 1.0) < 1e-9
    st = __import__('json').loads((tmp_path / 'rewards_state.json').read_text())
    assert st['positions']['c1']['inv'] == 10.0 and st['portfolio'] == port     # bid 0.48 filled, portfolio frozen


def test_realistic_ledger_rules(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live
    port = [{'cond': 'c1', 'question': 'Q', 'yes': 'y', 'no': 'n', 'rate': 24.0, 'v': 4.0, 'min_size': 10.0,
             'd': 0.02, 'asset': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', 'collateral': 9.6,
             'est_reward_day': 24.0, 'end': '2027-01-01'}]
    monkeypatch.setattr(live, 'choose', lambda http, log: port)
    books = {}
    monkeypatch.setattr(bt, 'fetch_books', lambda toks, http: books)

    def at(mid, comp=0.0):
        books.clear()
        books.update({'y': {'bids': [{'price': f'{mid - 0.01:.2f}', 'size': str(comp or 5)}],
                            'asks': [{'price': f'{mid + 0.01:.2f}', 'size': str(comp or 5)}]},
                      'n': {'bids': [{'price': f'{1 - mid - 0.01:.2f}', 'size': str(comp or 5)}],
                            'asks': [{'price': f'{1 - mid + 0.01:.2f}', 'size': str(comp or 5)}]}})
    t0 = 1_790_000_000
    trades = []
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: trades)
    at(0.50)
    live.step(tmp_path, now=t0, http=object())
    # 1 h later: alone, nothing moved -> both ledgers earn the full hour; a touch of our bid fills only R4
    trades[:] = [(t0 + 10, 0.48, 50)]
    tick = live.step(tmp_path, now=t0 + 3600, http=object())
    assert abs(tick['reward_tick'] - 1.0) < 1e-9 and abs(tick['real_reward_total'] - 1.0) < 1e-9
    assert tick['fills_total'] == 0 and tick['real_fills_total'] == 1
    # competitors arrive: realistic uses the lower (new) share, optimistic the new one too
    trades[:] = []
    at(0.50, comp=1000)
    t2 = live.step(tmp_path, now=t0 + 7200, http=object())
    assert t2['real_reward_total'] - 1.0 <= t2['reward_tick'] + 1e-9
    # the midpoint jumps 5 cents (> v - d = 2): optimistic still earns, realistic does not
    at(0.55)
    t3 = live.step(tmp_path, now=t0 + 10800, http=object())
    assert t3['reward_tick'] > 0 and abs(t3['real_reward_total'] - t2['real_reward_total']) < 1e-9
    # the book disappears: values are kept, not dropped
    books.clear()
    t4 = live.step(tmp_path, now=t0 + 14400, http=object())
    assert abs(t4['real_mm_value'] - t3['real_mm_value']) < 1e-9 and abs(t4['mm_value'] - t3['mm_value']) < 1e-9


def test_fills_are_recorded_per_ledger(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live
    port = [{'cond': 'c1', 'question': 'Q', 'yes': 'y', 'no': 'n', 'rate': 24.0, 'v': 4.0, 'min_size': 10.0,
             'd': 0.02, 'asset': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', 'collateral': 9.6,
             'est_reward_day': 24.0, 'end': '2027-01-01'}]
    monkeypatch.setattr(live, 'choose', lambda http, log: port)
    book = {'y': {'bids': [{'price': '0.49', 'size': '5'}], 'asks': [{'price': '0.51', 'size': '5'}]},
            'n': {'bids': [], 'asks': []}}
    monkeypatch.setattr(bt, 'fetch_books', lambda toks, http: book)
    t0 = 1_790_000_000
    # one trade through our bid (both ledgers), one touching our ask (realistic only)
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: [(t0 + 100, 0.47, 50), (t0 + 200, 0.52, 50)])
    live.step(tmp_path, now=t0, http=object())
    live.step(tmp_path, now=t0 + 3600, http=object())
    st = __import__('json').loads((tmp_path / 'rewards_state.json').read_text())
    got = sorted((f['ledger'], f['side'], f['price']) for f in st['fills'])
    assert got == [('opt', 'BUY', 0.48), ('real', 'BUY', 0.48), ('real', 'SELL', 0.52)]


def test_backfill_fills_the_gap_before_the_realistic_ledger():
    from rewardbot import live
    h = [{'ts': 't0', 'reward_tick': 0.0, 'reward_total': 0.0, 'mm_value': 0.0, 'net': 0.0, 'avg_share': 0.3},
         {'ts': 't1', 'reward_tick': 1.0, 'reward_total': 1.0, 'mm_value': -0.5, 'net': 0.5, 'avg_share': 0.2,
          'fills_total': 2},
         {'ts': 't2', 'reward_tick': 1.0, 'reward_total': 2.0, 'mm_value': -0.5, 'net': 1.5, 'avg_share': 0.4,
          'fills_total': 2, 'real_reward_total': 0.0, 'real_mm_value': 0.0, 'real_net': 0.0, 'real_fills_total': 0},
         {'ts': 't3', 'reward_tick': 1.0, 'reward_total': 3.0, 'mm_value': -0.5, 'net': 2.5, 'avg_share': 0.4,
          'fills_total': 2, 'real_reward_total': 1.0, 'real_mm_value': 0.0, 'real_net': 1.0, 'real_fills_total': 0}]
    st = {'started': 't0', 'history': h, 'real_reward_total': 1.0}
    live.backfill_real(st)
    # t1: share fell 0.3 -> 0.2, R1 keeps the lower: full 1.0; t2: rose 0.2 -> 0.4, half: 0.5
    assert h[1]['real_reward_total'] == 1.0 and h[1]['real_net'] == 0.5
    assert h[2]['real_reward_total'] == 1.5 and h[2]['real_mm_value'] == -0.5 and h[2]['real_fills_total'] == 2
    assert h[3]['real_reward_total'] == 2.5 and h[3]['real_net'] == 2.0
    assert st['real_reward_total'] == 2.5 and st['real_backfill']['mm_value'] == -0.5
    live.backfill_real(st)                                   # idempotent
    assert st['real_reward_total'] == 2.5


def _cand(cond, mid, rate=24.0):
    return {'cond': cond, 'question': cond, 'yes': cond + 'y', 'no': cond + 'n', 'rate': rate, 'v': 4.0,
            'min_size': 10.0, 'end': '2027-01-01', 'mid': mid, 'rest_q': (0.0, 0.0)}


def test_v2_selection_skips_edges_and_trending_markets(monkeypatch):
    from rewardbot import backtest as bt, live_v2
    monkeypatch.setattr(bt, 'candidates', lambda http, log: [_cand('ok', 0.5), _cand('edge', 0.12), _cand('trend', 0.5)])
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: [(1, 0.3, 5), (2, 0.6, 5)] if c == 'trend' else [(1, 0.5, 5)])
    got = [c['cond'] for c in live_v2.choose(object(), 1_790_000_000, lambda *_: None)]
    assert got == ['ok']


def test_v2_rebalances_daily_and_closes_dropped_inventory(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live_v2
    t0 = 1_790_000_000
    pick = [[_cand('a', 0.5)]]
    monkeypatch.setattr(live_v2, 'choose', lambda http, now, log: [{k: v for k, v in c.items() if k not in ('mid', 'rest_q')}
                                                                 | {'d': 0.02, 'collateral': 9.6, 'est_reward_day': 24.0}
                                                                 for c in pick[0]])
    def books(toks, http):
        return {t: {'bids': [{'price': '0.49', 'size': '5'}], 'asks': [{'price': '0.51', 'size': '5'}]} for t in toks}
    monkeypatch.setattr(bt, 'fetch_books', books)
    trades = []
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: trades)
    live_v2.step(tmp_path, now=t0, http=object())
    trades[:] = [(t0 + 10, 0.48, 50), (t0 + 20, 0.47, 50)]        # touch, then through: cap 1 x min size
    t1 = live_v2.step(tmp_path, now=t0 + 3600, http=object())
    assert abs(t1['reward_tick'] - 1.0) < 1e-9 and t1['fills_total'] == 1
    st = __import__('json').loads((tmp_path / 'rewards_v2_state.json').read_text())
    assert st['positions']['a']['inv'] == 10.0
    # a day later market 'a' is replaced: its 10 shares are sold at the best bid 0.49
    trades[:] = []
    pick[0] = [_cand('b', 0.5)]
    t2 = live_v2.step(tmp_path, now=t0 + 3600 + 86400, http=object())
    st = __import__('json').loads((tmp_path / 'rewards_v2_state.json').read_text())
    assert t2['rebalanced'] and st['positions']['a']['inv'] == 0 and abs(st['positions']['a']['cash'] - 0.1) < 1e-9
    assert [c['cond'] for c in st['portfolio']] == ['b'] and st['rebalances'][-1]['dropped'] == 1
